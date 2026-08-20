"""import_bundle_service のDB非依存(pure)ヘルパーのユニットテスト

frontmatter分離・本文フィールド抽出・複合キーのパース・本文中の拡張cite参照抽出を
検証する。export_bundle_service._build_frontmatter/_build_body_textが書き出す
フォーマットとの往復整合性(export→import)も確認する。
"""
from src.services.export_bundle_service import _build_body_text, _build_frontmatter
from src.services.import_bundle_service import (
    _extract_composite_refs,
    _load_bundle_entities,
    _parse_body_fields,
    _parse_composite_key,
    _split_frontmatter,
)


class TestParseCompositeKey:
    def test_valid_key_is_parsed_into_instance_type_and_local_id(self):
        assert _parse_composite_key("team-a:M12") == ("team-a", "material", 12)

    def test_all_type_codes_map_to_expected_type_names(self):
        assert _parse_composite_key("team-a:T1")[1] == "topic"
        assert _parse_composite_key("team-a:D2")[1] == "decision"
        assert _parse_composite_key("team-a:L3")[1] == "log"
        assert _parse_composite_key("team-a:A4")[1] == "activity"
        assert _parse_composite_key("team-a:M5")[1] == "material"

    def test_hyphenated_instance_id_is_accepted(self):
        assert _parse_composite_key("levwell-team-a:M767") == ("levwell-team-a", "material", 767)

    def test_missing_colon_returns_none(self):
        assert _parse_composite_key("team-aM12") is None

    def test_unknown_type_code_returns_none(self):
        assert _parse_composite_key("team-a:Z12") is None

    def test_uppercase_instance_id_returns_none(self):
        assert _parse_composite_key("Team-A:M12") is None

    def test_non_string_input_returns_none(self):
        assert _parse_composite_key(None) is None
        assert _parse_composite_key(123) is None


class TestExtractCompositeRefs:
    def test_extracts_single_reference(self):
        text = "参照: {{cite:team-a:M12}} です"
        assert _extract_composite_refs(text) == ["team-a:M12"]

    def test_extracts_multiple_references_in_order(self):
        text = "{{cite:team-a:D1}} then {{cite:team-b:L2}}"
        assert _extract_composite_refs(text) == ["team-a:D1", "team-b:L2"]

    def test_reference_inside_fenced_code_block_is_ignored(self):
        text = "```\n{{cite:team-a:M12}}\n```\nreal: {{cite:team-a:M99}}"
        assert _extract_composite_refs(text) == ["team-a:M99"]

    def test_no_references_returns_empty_list(self):
        assert _extract_composite_refs("plain text, no refs here") == []


class TestSplitFrontmatter:
    def test_valid_frontmatter_is_split_from_body(self):
        text = "---\nccm_type: material\ntitle: Hello\n---\n\n# Hello\n\nbody text\n"
        fm, body = _split_frontmatter(text)
        assert fm == {"ccm_type": "material", "title": "Hello"}
        assert body == "\n# Hello\n\nbody text\n"

    def test_missing_leading_marker_returns_none(self):
        assert _split_frontmatter("ccm_type: material\n---\nbody") is None

    def test_missing_closing_marker_returns_none(self):
        assert _split_frontmatter("---\nccm_type: material\nbody without closing fence") is None

    def test_non_mapping_frontmatter_returns_none(self):
        # YAML的には妥当だがdictにならない(スカラー1個)フロントマター
        assert _split_frontmatter("---\njust a scalar\n---\nbody") is None


class TestParseBodyFieldsSingleField:
    def test_material_body_strips_h1_and_leading_blank_lines(self):
        body = "\n# Sample Material\n\nhello world\n"
        fields = _parse_body_fields("material", body)
        assert fields == {"content": "hello world"}

    def test_activity_body_uses_description_field(self):
        body = "\n# My Activity\n\nsome description text\n"
        fields = _parse_body_fields("activity", body)
        assert fields == {"description": "some description text"}

    def test_topic_body_uses_description_field(self):
        body = "\n# My Topic\n\ntopic description\n"
        fields = _parse_body_fields("topic", body)
        assert fields == {"description": "topic description"}

    def test_log_body_uses_content_field(self):
        body = "\n# Some Log\n\nlog content here\n"
        fields = _parse_body_fields("log", body)
        assert fields == {"content": "log content here"}

    def test_multiline_content_is_preserved(self):
        body = "\n# Title\n\nline one\nline two\nline three\n"
        fields = _parse_body_fields("material", body)
        assert fields["content"] == "line one\nline two\nline three"


class TestParseBodyFieldsDecision:
    def test_decision_and_reason_are_split(self):
        body = (
            "\n# Decision Title\n\n"
            "<!-- ccm:field decision -->\n"
            "## 決定\n"
            "We decided X\n\n"
            "<!-- ccm:field reason -->\n"
            "## 理由\n"
            "Because Y\n"
        )
        fields = _parse_body_fields("decision", body)
        assert fields == {"decision": "We decided X", "reason": "Because Y"}

    def test_multiline_decision_and_reason_are_preserved(self):
        body = (
            "\n# Title\n\n"
            "<!-- ccm:field decision -->\n"
            "## 決定\n"
            "line one\nline two\n\n"
            "<!-- ccm:field reason -->\n"
            "## 理由\n"
            "reason line one\nreason line two\n"
        )
        fields = _parse_body_fields("decision", body)
        assert fields["decision"] == "line one\nline two"
        assert fields["reason"] == "reason line one\nreason line two"

    def test_missing_markers_falls_back_to_whole_body_as_decision(self):
        body = "\n# Title\n\njust some unstructured text\n"
        fields = _parse_body_fields("decision", body)
        assert fields["decision"] == "# Title\n\njust some unstructured text"
        assert fields["reason"] == ""


class TestExportImportRoundTrip:
    """export_bundle_serviceが書き出すfrontmatter/body形式を、import側がそのまま復元できることを確認する。"""

    def test_material_round_trips_through_export_and_import_parsers(self):
        frontmatter = _build_frontmatter(
            etype="material",
            composite_key="team-a:M12",
            title="Sample Material",
            tags=["domain:test"],
            created_at="2026-01-01 00:00:00",
            updated_at=None,
            retracted_at=None,
            belongs_to_keys=[],
            related_keys=[],
            supersedes=None,
            depends_on_keys=None,
            source="test",
            status=None,
        )
        body_text = _build_body_text("material", "Sample Material", {"content": "hello world\nsecond line"})
        file_content = frontmatter + "\n" + body_text

        fm, body = _split_frontmatter(file_content)
        assert fm["ccm_key"] == "team-a:M12"
        assert fm["title"] == "Sample Material"
        fields = _parse_body_fields("material", body)
        assert fields["content"] == "hello world\nsecond line"

    def test_decision_round_trips_through_export_and_import_parsers(self):
        frontmatter = _build_frontmatter(
            etype="decision",
            composite_key="team-a:D5",
            title="Decision Title",
            tags=["domain:test"],
            created_at="2026-01-01 00:00:00",
            updated_at=None,
            retracted_at=None,
            belongs_to_keys=["team-a:T1"],
            related_keys=[],
            supersedes=[],
            depends_on_keys=None,
            source=None,
            status=None,
        )
        body_text = _build_body_text(
            "decision", "Decision Title", {"decision": "We chose X", "reason": "Because Y matters"}
        )
        file_content = frontmatter + "\n" + body_text

        fm, body = _split_frontmatter(file_content)
        assert fm["ccm_key"] == "team-a:D5"
        fields = _parse_body_fields("decision", body)
        assert fields["decision"] == "We chose X"
        assert fields["reason"] == "Because Y matters"


class TestLoadBundleEntitiesPathGuard:
    """manifest.entities[].pathがbundle_root外を指す場合に読み込みを拒否することを確認する。"""

    def _write_material_file(self, path, key="team-a:M1", title="Sample", content="hello"):
        frontmatter = _build_frontmatter(
            etype="material",
            composite_key=key,
            title=title,
            tags=["domain:test"],
            created_at="2026-01-01 00:00:00",
            updated_at=None,
            retracted_at=None,
            belongs_to_keys=[],
            related_keys=[],
            supersedes=None,
            depends_on_keys=None,
            source="test",
            status=None,
        )
        body_text = _build_body_text("material", title, {"content": content})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frontmatter + "\n" + body_text, encoding="utf-8")

    def test_path_within_bundle_root_is_loaded_normally(self, tmp_path):
        bundle_root = tmp_path / "bundle"
        self._write_material_file(bundle_root / "material" / "M1.md")

        parsed, errors = _load_bundle_entities(
            str(bundle_root), [{"key": "team-a:M1", "path": "material/M1.md"}]
        )

        assert errors == []
        assert parsed["team-a:M1"]["fields"]["content"] == "hello"

    def test_relative_parent_traversal_path_is_rejected(self, tmp_path):
        bundle_root = tmp_path / "bundle"
        bundle_root.mkdir()
        self._write_material_file(tmp_path / "secret.md")

        parsed, errors = _load_bundle_entities(
            str(bundle_root), [{"key": "team-a:M1", "path": "../secret.md"}]
        )

        assert parsed == {}
        assert errors == [{"key": "team-a:M1", "error": "path_outside_bundle"}]

    def test_absolute_path_escaping_bundle_root_is_rejected(self, tmp_path):
        bundle_root = tmp_path / "bundle"
        bundle_root.mkdir()
        outside = tmp_path / "secret.md"
        self._write_material_file(outside)

        parsed, errors = _load_bundle_entities(
            str(bundle_root), [{"key": "team-a:M1", "path": str(outside)}]
        )

        assert parsed == {}
        assert errors == [{"key": "team-a:M1", "error": "path_outside_bundle"}]
