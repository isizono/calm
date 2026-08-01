"""scripts/check_unit_subprocess_placement.py の判定ロジックを検証する unit test。"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.check_unit_subprocess_placement import (  # noqa: E402
    find_violations,
    load_allowlist,
)

# フィクスチャに書き込む呼び出し構文は文字列連結で組み立てる。このテストファイル
# 自身も tests/unit/ 配下にあり、対象パターンをリテラルのまま書くと
# check_unit_subprocess_placement.py 自身のスキャン対象にもなってしまうため。
_SUBPROCESS_RUN_CALL = "subprocess" + ".run(['ls'])"
_BARE_POPEN_CALL = "Po" + "pen(['ls'])"


class TestLoadAllowlist:
    def test_parses_names_and_skips_comments_and_blank_lines(self, tmp_path: Path):
        allowlist_path = tmp_path / "allowlist.txt"
        allowlist_path.write_text(
            "# comment\n\ntest_a.py\n  test_b.py  \n# another comment\ntest_c.py\n"
        )
        assert load_allowlist(allowlist_path) == {"test_a.py", "test_b.py", "test_c.py"}

    def test_missing_file_returns_empty_set(self, tmp_path: Path):
        assert load_allowlist(tmp_path / "no-such-file.txt") == set()


class TestFindViolations:
    def test_detects_subprocess_run_call(self, tmp_path: Path):
        unit_dir = tmp_path / "unit"
        unit_dir.mkdir()
        (unit_dir / "test_new.py").write_text(f"import subprocess\n{_SUBPROCESS_RUN_CALL}\n")

        assert find_violations(unit_dir, allowlist=set()) == ["test_new.py"]

    def test_detects_bare_popen_call(self, tmp_path: Path):
        unit_dir = tmp_path / "unit"
        unit_dir.mkdir()
        (unit_dir / "test_new.py").write_text(f"from subprocess import Popen\n{_BARE_POPEN_CALL}\n")

        assert find_violations(unit_dir, allowlist=set()) == ["test_new.py"]

    def test_allowlisted_file_is_excluded(self, tmp_path: Path):
        unit_dir = tmp_path / "unit"
        unit_dir.mkdir()
        (unit_dir / "test_known.py").write_text(f"{_SUBPROCESS_RUN_CALL}\n")

        assert find_violations(unit_dir, allowlist={"test_known.py"}) == []

    def test_docstring_mention_without_call_syntax_is_not_flagged(self, tmp_path: Path):
        # docstring/コメント中の「subprocess.Popenが呼ばれる」のような散文的言及は
        # 開き括弧を伴わないため検出対象外。
        unit_dir = tmp_path / "unit"
        unit_dir.mkdir()
        (unit_dir / "test_mention_only.py").write_text(
            '"""正しい引数でsubprocess.Popenが呼ばれることを確認する"""\n'
            "def test_x():\n    pass\n"
        )

        assert find_violations(unit_dir, allowlist=set()) == []

    def test_non_python_files_are_ignored(self, tmp_path: Path):
        unit_dir = tmp_path / "unit"
        unit_dir.mkdir()
        (unit_dir / "notes.txt").write_text(f"{_SUBPROCESS_RUN_CALL}\n")

        assert find_violations(unit_dir, allowlist=set()) == []

    def test_missing_unit_dir_returns_empty_list(self, tmp_path: Path):
        assert find_violations(tmp_path / "no-such-dir", allowlist=set()) == []
