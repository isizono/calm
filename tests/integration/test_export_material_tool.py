"""export_material MCP tool の統合テスト"""
import os
import tempfile

import pytest
import yaml

from src.db import init_database
from src.services.activity_service import add_activity
from src.services.material_service import add_material, export_material_to_file
from src.services.retract_service import retract


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    """テスト用の一時DB。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def material_id(temp_db):
    """基本的な資材を1件作成して material_id を返す。"""
    result = add_material(
        title="Export Sample",
        content="# 見出し\n\nこれは export 用のサンプル本文です。\n",
        tags=["domain:test", "export-test"],
        source="unit test",
    )
    assert "error" not in result
    return result["material_id"]


@pytest.fixture
def material_with_related(temp_db):
    """関連付きの資材を作成し material_id を返す。"""
    activity = add_activity(
        title="Related Activity",
        description="for export test",
        tags=DEFAULT_TAGS,
        check_in=False,
    )
    activity_id = activity["activity_id"]
    result = add_material(
        title="Related Material",
        content="content body",
        tags=["domain:test"],
        source="unit test",
        related=[{"type": "activity", "ids": [activity_id]}],
    )
    assert "error" not in result
    return result["material_id"], activity_id


@pytest.fixture(autouse=True)
def _export_dir_under_tmp(monkeypatch, tmp_path):
    """書き込みガードの許可ルートを各テストの tmp_path に向ける。

    export_material_to_file は DEFAULT_EXPORT_DIR 配下のみ書き込みを許可する。
    テストの dest_path はすべて tmp_path 配下を指すため、許可ルートを tmp_path に
    合わせることで通常ケースを許可し、配下外テストは tmp_path 外を指して検証する。
    """
    monkeypatch.setattr("src.services.material_service.DEFAULT_EXPORT_DIR", str(tmp_path))


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """`---\\n...\\n---\\n\\n# ...` を frontmatter dict と body に分解する。"""
    assert text.startswith("---\n"), text[:20]
    end = text.index("\n---\n", 4)
    fm_body = text[4:end]
    remainder = text[end + len("\n---\n") :]
    return yaml.safe_load(fm_body), remainder


class TestExportDestPathVariants:
    def test_dest_path_omitted_uses_default_dir(self, material_id, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.services.material_service.DEFAULT_EXPORT_DIR", str(tmp_path)
        )
        result = export_material_to_file(material_id)
        assert "error" not in result
        assert result["material_id"] == material_id
        assert result["path"] == os.path.join(
            str(tmp_path), f"M-{material_id}-Export-Sample.md"
        )
        assert result["overwritten"] is False
        assert os.path.isfile(result["path"])

    def test_dest_path_directory_places_default_filename(self, material_id, tmp_path):
        result = export_material_to_file(material_id, dest_path=str(tmp_path))
        assert "error" not in result
        assert result["path"] == os.path.join(
            str(tmp_path), f"M-{material_id}-Export-Sample.md"
        )
        assert os.path.isfile(result["path"])

    def test_dest_path_file_uses_it_as_is(self, material_id, tmp_path):
        target = tmp_path / "sub" / "custom-name.md"
        result = export_material_to_file(material_id, dest_path=str(target))
        assert "error" not in result
        assert result["path"] == str(target)
        assert os.path.isfile(result["path"])


class TestOverwriteFlag:
    def test_first_write_overwritten_false(self, material_id, tmp_path):
        target = tmp_path / "out.md"
        result = export_material_to_file(material_id, dest_path=str(target))
        assert result["overwritten"] is False

    def test_second_write_overwritten_true(self, material_id, tmp_path):
        target = tmp_path / "out.md"
        export_material_to_file(material_id, dest_path=str(target))
        result = export_material_to_file(material_id, dest_path=str(target))
        assert result["overwritten"] is True


class TestOutputStructure:
    def test_body_starts_with_frontmatter_fence(self, material_id, tmp_path):
        target = tmp_path / "out.md"
        export_material_to_file(material_id, dest_path=str(target))
        text = _read(str(target))
        assert text.startswith("---\n")

    def test_h1_uses_title(self, material_id, tmp_path):
        target = tmp_path / "out.md"
        export_material_to_file(material_id, dest_path=str(target))
        text = _read(str(target))
        _, body = _split_frontmatter(text)
        assert body.startswith("\n# Export Sample\n\n")

    def test_frontmatter_contains_required_fields(self, material_id, tmp_path):
        target = tmp_path / "out.md"
        export_material_to_file(material_id, dest_path=str(target))
        fm, _ = _split_frontmatter(_read(str(target)))
        assert fm["material_id"] == material_id
        assert fm["title"] == "Export Sample"
        assert set(fm["tags"]) == {"domain:test", "export-test"}
        assert fm["source"] == "unit test"
        assert "created_at" in fm
        assert "updated_at" in fm
        assert fm["related"] == []

    def test_related_reflects_relations(self, material_with_related, tmp_path):
        mid, aid = material_with_related
        target = tmp_path / "out.md"
        export_material_to_file(mid, dest_path=str(target))
        fm, _ = _split_frontmatter(_read(str(target)))
        assert {"type": "activity", "id": aid} in fm["related"]

    def test_content_is_preserved(self, material_id, tmp_path):
        target = tmp_path / "out.md"
        export_material_to_file(material_id, dest_path=str(target))
        text = _read(str(target))
        assert "これは export 用のサンプル本文です。" in text


class TestErrors:
    def test_missing_material_returns_not_found(self, temp_db, tmp_path):
        result = export_material_to_file(999999, dest_path=str(tmp_path / "x.md"))
        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"

    def test_retracted_material_returns_not_found(self, material_id, tmp_path):
        r = retract("material", [material_id])
        assert "error" not in r, r
        assert material_id in r.get("success", [])
        result = export_material_to_file(
            material_id, dest_path=str(tmp_path / "x.md")
        )
        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"


class TestPathGuard:
    def test_path_outside_export_dir_is_rejected(self, material_id, tmp_path):
        with tempfile.TemporaryDirectory() as outside:
            target = os.path.join(outside, "escape.md")
            result = export_material_to_file(material_id, dest_path=target)
            assert "error" in result
            assert result["error"]["code"] == "VALIDATION_ERROR"
            assert not os.path.exists(target)

    def test_rejection_does_not_create_parent_dir(self, material_id, tmp_path):
        with tempfile.TemporaryDirectory() as outside:
            target = os.path.join(outside, "new-sub", "escape.md")
            result = export_material_to_file(material_id, dest_path=target)
            assert result["error"]["code"] == "VALIDATION_ERROR"
            assert not os.path.exists(os.path.join(outside, "new-sub"))

    def test_symlink_escape_is_rejected(self, material_id, tmp_path):
        with tempfile.TemporaryDirectory() as outside:
            link = tmp_path / "vault-link"
            os.symlink(outside, str(link))
            target = link / "escape.md"
            result = export_material_to_file(material_id, dest_path=str(target))
            assert "error" in result
            assert result["error"]["code"] == "VALIDATION_ERROR"
            assert not os.path.exists(os.path.join(outside, "escape.md"))

    def test_error_message_names_allowed_dir(self, material_id, tmp_path):
        with tempfile.TemporaryDirectory() as outside:
            target = os.path.join(outside, "escape.md")
            result = export_material_to_file(material_id, dest_path=target)
            assert str(tmp_path) in result["error"]["message"]


class TestMcpTool:
    def test_mcp_tool_end_to_end(self, material_id, tmp_path):
        """main.py 経由の MCP tool から呼んでも同じ挙動になる。"""
        from src import main as main_mod

        target = tmp_path / "mcp-out.md"
        # FastMCP 3.x で @mcp.tool() は元関数を返すのでそのまま呼べる
        result = main_mod.export_material(material_id, dest_path=str(target))
        assert "error" not in result
        assert result["path"] == str(target)
        assert os.path.isfile(result["path"])
        fm, _ = _split_frontmatter(_read(str(target)))
        assert fm["material_id"] == material_id
