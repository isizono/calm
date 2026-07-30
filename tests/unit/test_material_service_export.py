"""material_service の export_material_to_file 補助関数（DB非依存）ユニットテスト"""
import os

from src.services.material_service import (
    DEFAULT_EXPORT_DIR,
    SLUG_MAX_LEN,
    _build_frontmatter,
    _resolve_dest_path,
    _slugify_title,
)


class TestSlugifyTitle:
    def test_ascii_alnum_and_hyphen_are_preserved(self):
        assert _slugify_title("hello-world-42") == "hello-world-42"

    def test_spaces_and_symbols_are_replaced_with_hyphen(self):
        assert _slugify_title("hello world! foo/bar") == "hello-world-foo-bar"

    def test_consecutive_replacement_is_compressed(self):
        assert _slugify_title("a   b___c") == "a-b-c"

    def test_leading_and_trailing_hyphens_are_stripped(self):
        assert _slugify_title("---foo---") == "foo"

    def test_japanese_only_title_becomes_untitled(self):
        assert _slugify_title("設計メモ") == "untitled"

    def test_empty_string_becomes_untitled(self):
        assert _slugify_title("") == "untitled"

    def test_only_symbols_becomes_untitled(self):
        assert _slugify_title("***///") == "untitled"

    def test_truncated_to_max_length_without_trailing_hyphen(self):
        title = "a" * (SLUG_MAX_LEN + 20)
        slug = _slugify_title(title)
        assert len(slug) == SLUG_MAX_LEN
        assert not slug.endswith("-")

    def test_truncation_strips_trailing_hyphen(self):
        title = "a" * SLUG_MAX_LEN + " " + "b" * 10
        slug = _slugify_title(title)
        assert len(slug) <= SLUG_MAX_LEN
        assert not slug.endswith("-")


class TestResolveDestPath:
    def test_none_uses_default_export_dir(self):
        path = _resolve_dest_path(123, "hello", None)
        expected_prefix = os.path.expanduser(DEFAULT_EXPORT_DIR)
        assert path.startswith(expected_prefix + os.sep)
        assert path.endswith("M-123-hello.md")

    def test_existing_dir_places_default_filename_under_it(self, tmp_path):
        path = _resolve_dest_path(7, "foo bar", str(tmp_path))
        assert path == os.path.join(str(tmp_path), "M-7-foo-bar.md")

    def test_non_existing_path_is_used_as_file_path(self, tmp_path):
        target = tmp_path / "sub" / "custom.md"
        path = _resolve_dest_path(9, "irrelevant", str(target))
        assert path == str(target)

    def test_japanese_title_falls_back_to_untitled_in_default_filename(self, tmp_path):
        path = _resolve_dest_path(5, "設計メモ", str(tmp_path))
        assert path == os.path.join(str(tmp_path), "M-5-untitled.md")

    def test_expanduser_on_dest_path(self):
        path = _resolve_dest_path(1, "t", "~/nonexistent-cc-memory-test-file.md")
        assert not path.startswith("~")

    def test_relative_file_path_is_made_absolute(self):
        path = _resolve_dest_path(3, "t", "out.md")
        assert os.path.isabs(path)
        assert path == os.path.abspath("out.md")

    def test_relative_subdir_file_path_is_made_absolute(self):
        path = _resolve_dest_path(4, "t", os.path.join("sub", "out.md"))
        assert os.path.isabs(path)
        assert path == os.path.abspath(os.path.join("sub", "out.md"))


class TestBuildFrontmatter:
    def test_wraps_body_with_hyphen_fences(self):
        fm = _build_frontmatter(
            entity_id=1,
            title="t",
            tags=["a"],
            source="s",
            related=[],
            created_at="2026-01-01 00:00:00",
            updated_at="2026-01-01 00:00:00",
        )
        assert fm.startswith("---\n")
        assert fm.endswith("---\n")

    def test_yaml_content_contains_all_required_keys(self):
        import yaml

        fm = _build_frontmatter(
            entity_id=42,
            title="サンプル",
            tags=["domain:test", "feature"],
            source="unit-test",
            related=[{"type": "activity", "id": 100}],
            created_at="2026-06-01 12:00:00",
            updated_at="2026-06-02 12:00:00",
        )
        body = fm.strip().strip("-").strip()
        parsed = yaml.safe_load(body)
        assert parsed["material_id"] == 42
        assert parsed["title"] == "サンプル"
        assert parsed["tags"] == ["domain:test", "feature"]
        assert parsed["source"] == "unit-test"
        assert parsed["related"] == [{"type": "activity", "id": 100}]
        assert parsed["created_at"] == "2026-06-01 12:00:00"
        assert parsed["updated_at"] == "2026-06-02 12:00:00"
