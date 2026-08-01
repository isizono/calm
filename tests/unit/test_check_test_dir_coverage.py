"""scripts/check_test_dir_coverage.py の判定ロジックを検証する unit test。"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.check_test_dir_coverage import (  # noqa: E402
    discover_test_dirs,
    find_unreferenced_dirs,
    workflow_text,
)


class TestDiscoverTestDirs:
    def test_lists_direct_subdirectories_only(self, tmp_path: Path):
        tests_root = tmp_path / "tests"
        (tests_root / "unit").mkdir(parents=True)
        (tests_root / "e2e").mkdir()
        (tests_root / "unit" / "nested").mkdir()  # 直下ではないので対象外

        assert discover_test_dirs(tests_root) == ["e2e", "unit"]

    def test_excludes_hidden_and_pycache_dirs(self, tmp_path: Path):
        tests_root = tmp_path / "tests"
        (tests_root / "unit").mkdir(parents=True)
        (tests_root / "__pycache__").mkdir()
        (tests_root / ".pytest_cache").mkdir()

        assert discover_test_dirs(tests_root) == ["unit"]

    def test_excludes_files(self, tmp_path: Path):
        tests_root = tmp_path / "tests"
        tests_root.mkdir()
        (tests_root / "conftest.py").write_text("")
        (tests_root / "unit").mkdir()

        assert discover_test_dirs(tests_root) == ["unit"]

    def test_missing_tests_root_returns_empty_list(self, tmp_path: Path):
        assert discover_test_dirs(tmp_path / "no-such-dir") == []


class TestWorkflowText:
    def test_concatenates_yml_files(self, tmp_path: Path):
        workflows_dir = tmp_path / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "a.yml").write_text("path: tests/unit\n")
        (workflows_dir / "b.yml").write_text("path: tests/e2e\n")

        text = workflow_text(workflows_dir)
        assert "tests/unit" in text
        assert "tests/e2e" in text

    def test_missing_workflows_dir_returns_empty_string(self, tmp_path: Path):
        assert workflow_text(tmp_path / "no-such-dir") == ""


class TestFindUnreferencedDirs:
    def test_flags_directory_not_mentioned_anywhere(self):
        combined = "path: tests/unit tests/test_migrations\npath: tests/integration tests/e2e\n"
        unreferenced = find_unreferenced_dirs(["unit", "e2e", "services"], combined)
        assert unreferenced == ["services"]

    def test_empty_when_all_referenced(self):
        combined = "path: tests/unit tests/e2e\n"
        assert find_unreferenced_dirs(["unit", "e2e"], combined) == []

    def test_substring_match_does_not_require_word_boundary(self):
        # 判定はディレクトリ名を含む文字列マッチのみ。`tests/unit-extra` のような
        # 別ディレクトリを指すつもりの記述でも `tests/unit` は「参照あり」判定に
        # なる(意図: シンプルさ優先。誤検知より過検知緩和側に倒す設計)。
        combined = "path: tests/unit-extra\n"
        assert find_unreferenced_dirs(["unit"], combined) == []
