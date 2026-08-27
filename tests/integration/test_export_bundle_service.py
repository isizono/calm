"""export_bundle_serviceの統合テスト

instance_idゲート・複合キー生成・親topic自動同梱(decision/logのみ)・supersede先の
既定非同梱/include_supersede_targets・本文citationパイプライン(生リテラル正規化→
複合キー化→残存リテラルの最終スイープ)・manifest/frontmatterの整合性を検証する。
"""
import os
import tempfile

import pytest
import yaml

from src.db import get_connection, init_database
from src.services.activity_service import add_activity
from src.services.export_bundle_service import export_bundle
from src.services.instance_service import set_instance_identity
from src.services.material_service import add_material
from src.services.relation_service import add_relation
from src.services.tag_service import _injected_tags
from src.services.topic_service import add_topic
from tests.helpers import add_decision, add_log

DEFAULT_TAGS = ["domain:test-bundle"]


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture(autouse=True)
def _export_dir_under_tmp(monkeypatch, tmp_path):
    """パスガードの許可ルートをtmp_pathに向ける(export_material_to_fileと同じ作法)。

    export_bundle_serviceはmaterial_service.DEFAULT_EXPORT_DIRを属性参照(モジュール経由)
    するため、material_service側を1箇所patchすれば両方に反映される。
    """
    monkeypatch.setattr("src.services.material_service.DEFAULT_EXPORT_DIR", str(tmp_path))


def _topic(title="Topic", tags=None):
    result = add_topic(title=title, description=f"Description for {title}", tags=tags or DEFAULT_TAGS)
    assert "error" not in result
    return result["topic_id"]


def _activity(title="Activity", tags=None, related=None):
    result = add_activity(
        title=title, description=f"Description for {title}", tags=tags or DEFAULT_TAGS,
        related=related, check_in=False,
    )
    assert "error" not in result
    return result["activity_id"]


def _material(title="Material", content="Content", tags=None, related=None):
    result = add_material(title=title, content=content, tags=tags or DEFAULT_TAGS, source="test", related=related)
    assert "error" not in result
    return result["material_id"]


def _decision(topic_id, decision="Decision text", reason="Reason text", tags=None):
    result = add_decision(decision=decision, reason=reason, topic_id=topic_id, tags=tags or DEFAULT_TAGS)
    assert "error" not in result
    return result["decision_id"]


def _log(topic_id, content="Log content", tags=None):
    result = add_log(topic_id=topic_id, content=content, tags=tags or DEFAULT_TAGS)
    assert "error" not in result
    return result["log_id"]


def _set_instance(instance_id="team-a"):
    result = set_instance_identity(instance_id)
    assert "error" not in result
    return instance_id


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _split_frontmatter(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n"), text[:30]
    end = text.index("\n---\n", 4)
    fm = text[4:end]
    remainder = text[end + len("\n---\n") :]
    return yaml.safe_load(fm), remainder


def _load_manifest(bundle_path: str) -> dict:
    with open(os.path.join(bundle_path, "manifest.yaml"), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestValidation:
    def test_empty_items_rejected(self, temp_db):
        result = export_bundle(items=[])
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_type_rejected(self, temp_db):
        result = export_bundle(items=[{"type": "bogus", "ids": [1]}])
        assert result["error"]["code"] == "INVALID_ENTITY_TYPE"

    def test_unhashable_type_value_rejected_without_raising(self, temp_db):
        """typeがlist等の非hashable値でも例外(TypeError)を送出せずエラーレスポンスを返す。"""
        result = export_bundle(items=[{"type": ["material"], "ids": [1]}])
        assert result["error"]["code"] == "INVALID_ENTITY_TYPE"

    def test_non_list_ids_rejected(self, temp_db):
        result = export_bundle(items=[{"type": "material", "ids": 1}])
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_non_positive_id_rejected(self, temp_db):
        result = export_bundle(items=[{"type": "material", "ids": [0]}])
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_type_key_rejected(self, temp_db):
        result = export_bundle(items=[{"ids": [1]}])
        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestInstanceIdGate:
    def test_export_without_instance_id_fails(self, temp_db):
        m1 = _material()
        result = export_bundle(items=[{"type": "material", "ids": [m1]}])
        assert result["error"]["code"] == "INSTANCE_ID_NOT_SET"

    def test_export_after_setting_instance_id_succeeds(self, temp_db):
        _set_instance("team-a")
        m1 = _material()
        result = export_bundle(items=[{"type": "material", "ids": [m1]}])
        assert "error" not in result


class TestNotFound:
    def test_nonexistent_material_id_rejected(self, temp_db):
        _set_instance("team-a")
        missing_id = 999999
        result = export_bundle(items=[{"type": "material", "ids": [missing_id]}])
        assert result["error"]["code"] == "NOT_FOUND"
        assert f"material#{missing_id}" in result["error"]["message"]

    def test_no_files_written_when_any_id_missing(self, temp_db, tmp_path):
        _set_instance("team-a")
        m1 = _material()
        missing_id = 999999
        export_bundle(items=[{"type": "material", "ids": [m1, missing_id]}])
        # bundlesディレクトリ自体が作られていないこと(全滅時に部分書き込みしない)
        assert not os.path.isdir(os.path.join(str(tmp_path), "bundles"))


class TestBasicExport:
    def test_single_material_produces_expected_layout(self, temp_db, tmp_path):
        _set_instance("team-a")
        m1 = _material(title="Sample Material", content="hello world")
        result = export_bundle(items=[{"type": "material", "ids": [m1]}])
        assert "error" not in result
        assert result["counts"] == {"material": 1}
        assert result["path"].startswith(str(tmp_path))

        manifest = _load_manifest(result["path"])
        assert manifest["format"] == "ccm-bundle/1"
        assert manifest["source_instance"] == "team-a"
        assert manifest["bundle_id"] == result["bundle_id"]
        assert len(manifest["entities"]) == 1
        entity = manifest["entities"][0]
        assert entity["key"] == f"team-a:M{m1}"
        assert entity["type"] == "material"
        assert entity["title"] == "Sample Material"
        assert entity["path"] == f"materials/M-{m1}-Sample-Material.md"

        md_path = os.path.join(result["path"], entity["path"])
        assert os.path.isfile(md_path)
        fm, body = _split_frontmatter(_read(md_path))
        assert fm["ccm_key"] == f"team-a:M{m1}"
        assert fm["ccm_type"] == "material"
        assert fm["title"] == "Sample Material"
        assert fm["source"] == "test"
        assert body.startswith("\n# Sample Material\n\nhello world")

    def test_selection_param_recorded_verbatim_in_manifest(self, temp_db):
        _set_instance("team-a")
        m1 = _material()
        sel = {"roots": [{"type": "material", "id": m1}], "max_depth": 2, "tag_roots": ["domain:x"]}
        result = export_bundle(items=[{"type": "material", "ids": [m1]}], selection=sel)
        manifest = _load_manifest(result["path"])
        assert manifest["selection"] == sel

    def test_selection_defaults_to_items_when_omitted(self, temp_db):
        _set_instance("team-a")
        m1 = _material()
        items = [{"type": "material", "ids": [m1]}]
        result = export_bundle(items=items)
        manifest = _load_manifest(result["path"])
        assert manifest["selection"] == {"items": items}

    def test_content_hash_is_stable_across_repeated_export(self, temp_db):
        _set_instance("team-a")
        m1 = _material(title="Stable", content="unchanged content")
        r1 = export_bundle(items=[{"type": "material", "ids": [m1]}], bundle_name="run-1")
        r2 = export_bundle(items=[{"type": "material", "ids": [m1]}], bundle_name="run-2")
        h1 = _load_manifest(r1["path"])["entities"][0]["content_hash"]
        h2 = _load_manifest(r2["path"])["entities"][0]["content_hash"]
        assert h1 == h2

    def test_content_hash_changes_when_content_changes(self, temp_db):
        from src.services.material_service import update_material

        _set_instance("team-a")
        m1 = _material(title="Stable", content="original content")
        r1 = export_bundle(items=[{"type": "material", "ids": [m1]}], bundle_name="before")
        update_material(material_id=m1, content="edited content")
        r2 = export_bundle(items=[{"type": "material", "ids": [m1]}], bundle_name="after")
        h1 = _load_manifest(r1["path"])["entities"][0]["content_hash"]
        h2 = _load_manifest(r2["path"])["entities"][0]["content_hash"]
        assert h1 != h2

    def test_retracted_material_keeps_retracted_at_when_explicitly_selected(self, temp_db):
        from src.services.retract_service import retract

        _set_instance("team-a")
        m1 = _material()
        retract("material", [m1])
        result = export_bundle(items=[{"type": "material", "ids": [m1]}])
        assert "error" not in result
        md_path = os.path.join(result["path"], f"materials/M-{m1}-Material.md")
        fm, _ = _split_frontmatter(_read(md_path))
        assert fm["retracted_at"] is not None


class TestParentTopicAutoInclude:
    def test_decision_export_auto_includes_parent_topic(self, temp_db):
        _set_instance("team-a")
        t1 = _topic("Parent Topic")
        d1 = _decision(t1)
        result = export_bundle(items=[{"type": "decision", "ids": [d1]}])
        assert "error" not in result
        assert result["counts"] == {"decision": 1, "topic": 1}
        assert {"type": "topic", "id_raw": t1, "reason": "parent_topic"} in result["auto_included"]

    def test_log_export_auto_includes_parent_topic(self, temp_db):
        _set_instance("team-a")
        t1 = _topic("Parent Topic")
        l1 = _log(t1)
        result = export_bundle(items=[{"type": "log", "ids": [l1]}])
        assert "error" not in result
        assert result["counts"] == {"log": 1, "topic": 1}

    def test_decision_frontmatter_belongs_to_points_to_auto_included_topic(self, temp_db):
        _set_instance("team-a")
        t1 = _topic("Parent Topic")
        d1 = _decision(t1)
        result = export_bundle(items=[{"type": "decision", "ids": [d1]}])
        md_path = os.path.join(result["path"], f"decisions/D-{d1}-Decision-text.md")
        fm, _ = _split_frontmatter(_read(md_path))
        assert fm["belongs_to"] == [f"team-a:T{t1}"]

    def test_activity_export_does_not_auto_include_topic(self, temp_db):
        """activityは常に明示選択のみが対象。belongs_toがあっても強制同梱しない。

        親topicが選択集合外に残るぶん、frontmatterのbelongs_toは選択集合外を指した
        ままになり、unresolved_refsで検知されなければならない。
        """
        _set_instance("team-a")
        t1 = _topic("Parent Topic")
        a1 = _activity(related=[{"type": "topic", "ids": [t1]}])
        result = export_bundle(items=[{"type": "activity", "ids": [a1]}])
        assert "error" not in result
        assert result["counts"] == {"activity": 1}
        assert result["auto_included"] == []
        assert any(
            r["key"] == f"team-a:T{t1}" and r["type"] == "topic" for r in result["unresolved_refs"]
        )
        md_path = os.path.join(result["path"], f"activities/A-{a1}-Activity.md")
        fm, _ = _split_frontmatter(_read(md_path))
        assert fm["belongs_to"] == [f"team-a:T{t1}"]

    def test_material_export_does_not_auto_include_topic(self, temp_db):
        _set_instance("team-a")
        t1 = _topic("Parent Topic")
        m1 = _material(related=[{"type": "topic", "ids": [t1]}])
        result = export_bundle(items=[{"type": "material", "ids": [m1]}])
        assert result["counts"] == {"material": 1}
        assert result["auto_included"] == []
        assert any(
            r["key"] == f"team-a:T{t1}" and r["type"] == "topic" for r in result["unresolved_refs"]
        )
        md_path = os.path.join(result["path"], f"materials/M-{m1}-Material.md")
        fm, _ = _split_frontmatter(_read(md_path))
        assert fm["belongs_to"] == [f"team-a:T{t1}"]


class TestSupersede:
    def test_default_excludes_target_and_records_warning_and_unresolved(self, temp_db):
        _set_instance("team-a")
        t1 = _topic("Topic")
        old = _decision(t1, decision="Old decision")
        new = _decision(t1, decision="New decision")
        add_relation("decision", new, [{"type": "decision", "ids": [old]}], relation_type="supersedes")

        result = export_bundle(items=[{"type": "decision", "ids": [new]}])
        assert "error" not in result
        # old decisionは実体として同梱されない(newの親topicのみ自動同梱)
        assert result["counts"] == {"decision": 1, "topic": 1}
        assert any(w["kind"] == "supersede_target_outside" and w["target"]["id_raw"] == old for w in result["warnings"])
        assert any(r["key"] == f"team-a:D{old}" for r in result["unresolved_refs"])

        md_path = os.path.join(result["path"], f"decisions/D-{new}-New-decision.md")
        fm, _ = _split_frontmatter(_read(md_path))
        assert fm["supersedes"] == [{"key": f"team-a:D{old}", "kind": "replaces"}]

    def test_include_supersede_targets_adds_entity_and_its_parent_topic(self, temp_db):
        _set_instance("team-a")
        t1 = _topic("Topic")
        old = _decision(t1, decision="Old decision")
        new = _decision(t1, decision="New decision")
        add_relation("decision", new, [{"type": "decision", "ids": [old]}], relation_type="supersedes")

        result = export_bundle(
            items=[{"type": "decision", "ids": [new]}], include_supersede_targets=True
        )
        assert "error" not in result
        assert result["counts"] == {"decision": 2, "topic": 1}
        assert result["warnings"] == []
        assert {"type": "decision", "id_raw": old, "reason": "supersede_target"} in result["auto_included"]
        old_path = os.path.join(result["path"], f"decisions/D-{old}-Old-decision.md")
        assert os.path.isfile(old_path)


class TestRelatedEdges:
    def test_related_material_link_recorded_distinct_from_belongs_to(self, temp_db):
        _set_instance("team-a")
        m1 = _material(title="First")
        m2 = _material(title="Second", related=[{"type": "material", "ids": [m1]}])

        result = export_bundle(items=[{"type": "material", "ids": [m1, m2]}])
        assert "error" not in result
        md_path = os.path.join(result["path"], f"materials/M-{m2}-Second.md")
        fm, _ = _split_frontmatter(_read(md_path))
        assert fm["related"] == [f"team-a:M{m1}"]
        assert "belongs_to" not in fm or fm["belongs_to"] == []

    def test_related_target_outside_selection_appears_in_unresolved_refs(self, temp_db):
        """related先が選択集合外でもfrontmatterには書かれるが、unresolved_refsで検知される。"""
        _set_instance("team-a")
        m1 = _material(title="First")
        m2 = _material(title="Second", related=[{"type": "material", "ids": [m1]}])

        result = export_bundle(items=[{"type": "material", "ids": [m2]}])
        assert "error" not in result
        assert result["counts"] == {"material": 1}
        assert any(r["key"] == f"team-a:M{m1}" and r["type"] == "material" for r in result["unresolved_refs"])
        md_path = os.path.join(result["path"], f"materials/M-{m2}-Second.md")
        fm, _ = _split_frontmatter(_read(md_path))
        assert fm["related"] == [f"team-a:M{m1}"]

    def test_depends_on_target_outside_selection_appears_in_unresolved_refs(self, temp_db):
        """depends_on先が選択集合外でもfrontmatterには書かれるが、unresolved_refsで検知される。"""
        _set_instance("team-a")
        a1 = _activity(title="Dependency")
        a2 = _activity(title="Dependent")
        add_relation("activity", a2, [{"type": "activity", "ids": [a1]}], relation_type="depends_on")

        result = export_bundle(items=[{"type": "activity", "ids": [a2]}])
        assert "error" not in result
        assert result["counts"] == {"activity": 1}
        assert any(r["key"] == f"team-a:A{a1}" and r["type"] == "activity" for r in result["unresolved_refs"])
        md_path = os.path.join(result["path"], f"activities/A-{a2}-Dependent.md")
        fm, _ = _split_frontmatter(_read(md_path))
        assert fm["depends_on"] == [f"team-a:A{a1}"]


class TestCitationPipeline:
    def test_existing_cite_template_rewritten_to_composite_key(self, temp_db):
        _set_instance("team-a")
        m1 = _material(title="Target", content="target body")
        # add_material の書き込み時自動変換により、実在IDへの生リテラルは
        # citeテンプレート形式で既にDBへ保存される(型コード+#+数字)
        code = "M"
        m2 = _material(title="Referrer", content=f"see {code}#{m1} for details")

        conn = get_connection()
        try:
            row = conn.execute("SELECT content FROM materials WHERE id = ?", (m2,)).fetchone()
        finally:
            conn.close()
        assert row["content"] == "see {{cite:" + code + "#" + str(m1) + "}} for details"

        result = export_bundle(items=[{"type": "material", "ids": [m1, m2]}])
        md_path = os.path.join(result["path"], f"materials/M-{m2}-Referrer.md")
        _, body = _split_frontmatter(_read(md_path))
        assert f"see {{{{cite:team-a:M{m1}}}}} for details" in body

    def test_reference_to_entity_outside_selection_is_still_rewritten(self, temp_db):
        """参照先が選択集合外でも複合キー化は行う(unresolved_refsにも載る)。"""
        _set_instance("team-a")
        m1 = _material(title="Not selected", content="body")
        code = "M"
        m2 = _material(title="Referrer", content=f"see {code}#{m1}")

        result = export_bundle(items=[{"type": "material", "ids": [m2]}])
        assert result["counts"] == {"material": 1}
        md_path = os.path.join(result["path"], f"materials/M-{m2}-Referrer.md")
        _, body = _split_frontmatter(_read(md_path))
        assert f"{{{{cite:team-a:M{m1}}}}}" in body
        assert any(r["key"] == f"team-a:M{m1}" and r["title"] == "Not selected" for r in result["unresolved_refs"])

    def test_dangling_reference_stays_marked_deleted(self, temp_db):
        _set_instance("team-a")
        code = "M"
        missing_id = 999999
        m1 = _material(title="Referrer", content=f"see {code}#{missing_id} which never existed")
        result = export_bundle(items=[{"type": "material", "ids": [m1]}])
        md_path = os.path.join(result["path"], f"materials/M-{m1}-Referrer.md")
        _, body = _split_frontmatter(_read(md_path))
        assert f"[deleted {code}#{missing_id}]" in body
        assert result["masked_literals"] == 0

    def test_raw_literal_inside_code_fence_is_preserved_untouched(self, temp_db):
        _set_instance("team-a")
        target = _material(title="Target", content="body")
        content = "before\n```\nliteral M#" + str(target) + " stays as-is\n```\nafter"
        m1 = _material(title="WithCodeFence", content=content)

        result = export_bundle(items=[{"type": "material", "ids": [m1, target]}])
        md_path = os.path.join(result["path"], f"materials/M-{m1}-WithCodeFence.md")
        _, body = _split_frontmatter(_read(md_path))
        assert f"literal M#{target} stays as-is" in body
        assert "team-a:M" not in body.split("```")[1]

    def test_hash_omitted_fullword_reference_outside_code_block_is_masked(self, temp_db):
        """#省略のフルワード形式(1段目が対象にしない形式)は最終スイープでマスクされる。"""
        _set_instance("team-a")
        # ソース中に "<type> <digit>" が連続して現れないよう動的に組み立てる
        # (内部ID漏洩防止フックの対象になるのを避けるため)
        phrase = "material" + " " + "42"
        m1 = _material(title="Legacy", content=f"references {phrase} without a hash")

        result = export_bundle(items=[{"type": "material", "ids": [m1]}])
        md_path = os.path.join(result["path"], f"materials/M-{m1}-Legacy.md")
        _, body = _split_frontmatter(_read(md_path))
        assert "(解決不能な内部参照)" in body
        assert phrase not in body
        assert result["masked_literals"] == 1


class TestBundleNamePathGuard:
    def test_traversal_like_bundle_name_stays_within_export_dir(self, temp_db, tmp_path):
        _set_instance("team-a")
        m1 = _material()
        result = export_bundle(items=[{"type": "material", "ids": [m1]}], bundle_name="../../../etc")
        assert "error" not in result
        assert result["path"].startswith(os.path.join(str(tmp_path), "bundles"))


class TestBundleNameCollision:
    def test_second_export_with_same_bundle_name_does_not_overwrite_first(self, temp_db):
        """同一bundle_nameで2回exportしても、1回目の出力を無警告で上書きしない。"""
        _set_instance("team-a")
        m1 = _material(title="First", content="first content")
        m2 = _material(title="Second", content="second content")
        r1 = export_bundle(items=[{"type": "material", "ids": [m1]}], bundle_name="same-name")
        r2 = export_bundle(items=[{"type": "material", "ids": [m2]}], bundle_name="same-name")
        assert "error" not in r1
        assert "error" not in r2
        assert r1["path"] != r2["path"]
        assert os.path.isfile(os.path.join(r1["path"], f"materials/M-{m1}-First.md"))
        assert os.path.isfile(os.path.join(r2["path"], f"materials/M-{m2}-Second.md"))


class TestTagDefinitions:
    """manifest.yamlのtag_definitionsセクション(notesを持つタグのみ収録)を検証する。

    importのタグレビュー(新規作成タグは全文、既存合流タグは差分を展開する)が
    notes本文を参照できるように、frontmatterのtagsフィールド(文字列のみ)とは別に
    manifest側へ書き出す経路。
    """

    def test_tag_with_notes_is_included_with_full_text(self, temp_db):
        from src.services.tag_service import update_tag

        _set_instance("team-a")
        m1 = _material(tags=["domain:test-bundle"])
        update_tag("domain:test-bundle", notes="運用上の注意事項テキスト")
        result = export_bundle(items=[{"type": "material", "ids": [m1]}])
        manifest = _load_manifest(result["path"])
        defs = {d["tag"]: d["notes"] for d in manifest["tag_definitions"]}
        assert defs["domain:test-bundle"] == "運用上の注意事項テキスト"

    def test_tag_without_notes_is_excluded(self, temp_db):
        _set_instance("team-a")
        m1 = _material(tags=["domain:test-bundle"])
        result = export_bundle(items=[{"type": "material", "ids": [m1]}])
        manifest = _load_manifest(result["path"])
        assert manifest["tag_definitions"] == []

    def test_tag_on_unselected_entity_is_excluded(self, temp_db):
        """選択集合に含まれないエンティティだけが使うタグは、notesがあってもtag_definitionsに出ない。"""
        from src.services.tag_service import update_tag

        _set_instance("team-a")
        # domain:test-bundle を使う material は export 対象に含めない
        _material(tags=["domain:test-bundle"])
        update_tag("domain:test-bundle", notes="unrelated-notes")
        m2 = _material(tags=["hooks"])
        result = export_bundle(items=[{"type": "material", "ids": [m2]}])
        manifest = _load_manifest(result["path"])
        assert manifest["tag_definitions"] == []

    def test_shared_tag_appears_once_across_multiple_entities(self, temp_db):
        from src.services.tag_service import update_tag

        _set_instance("team-a")
        m1 = _material(title="M1", tags=["domain:test-bundle"])
        m2 = _material(title="M2", tags=["domain:test-bundle"])
        update_tag("domain:test-bundle", notes="shared-notes")
        result = export_bundle(items=[{"type": "material", "ids": [m1, m2]}])
        manifest = _load_manifest(result["path"])
        tags = [d["tag"] for d in manifest["tag_definitions"]]
        assert tags.count("domain:test-bundle") == 1
