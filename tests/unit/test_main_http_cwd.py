"""src.main._ensure_project_root_cwd の動作テスト。

HTTP server 起動時に cwd をプロジェクトルートへ強制固定する関数。
起動時 cwd (e.g. worktree内) に残置されると当該パスが消えたとき
subprocess呼び出しが失敗するため、構造的に project_root へ寄せる。
"""
import ast
from pathlib import Path

import pytest

from src import main as main_module


class TestEnsureProjectRootCwd:
    def test_chdir_invoked_with_project_root(self, monkeypatch):
        """os.chdir が _ensure_project_root_cwd 内で1回、project_root を引数に呼ばれる。"""
        called_with: list = []

        def fake_chdir(path):
            called_with.append(path)

        monkeypatch.setattr(main_module.os, "chdir", fake_chdir)
        returned = main_module._ensure_project_root_cwd()

        assert len(called_with) == 1
        assert called_with[0] == returned

    def test_returns_path_corresponding_to_src_main_parent_parent(self, monkeypatch):
        """戻り値が src/main.py の親の親 (= リポジトリ root) になる。"""
        monkeypatch.setattr(main_module.os, "chdir", lambda _path: None)
        returned = main_module._ensure_project_root_cwd()
        expected = Path(main_module.__file__).resolve().parent.parent
        assert returned == expected

    def test_returned_path_contains_src_directory(self, monkeypatch):
        """戻り値の直下に src/ ディレクトリが存在する (project_root の妥当性)。"""
        monkeypatch.setattr(main_module.os, "chdir", lambda _path: None)
        returned = main_module._ensure_project_root_cwd()
        assert (returned / "src").is_dir()
        assert (returned / "src" / "main.py").is_file()


class TestShutdownServerSignalChoice:
    """_shutdown_serverは`if __name__ == "__main__":`ブロック内のクロージャで
    テストから直接呼べないため、ソースをASTで検査する。os.kill(os.getpid(), ...)は
    Windowsでは TerminateProcess 相当になり finally の release() が走らない。
    """

    def _source_tree(self):
        source = Path(main_module.__file__).read_text(encoding="utf-8")
        return ast.parse(source)

    def test_does_not_use_os_kill_on_own_pid(self):
        for node in ast.walk(self._source_tree()):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "kill"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                continue
            if node.args and (
                isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Attribute)
                and node.args[0].func.attr == "getpid"
            ):
                pytest.fail("os.kill(os.getpid(), ...) が検出された（Windowsでrelease()が走らない）")

    def test_uses_raise_signal_for_shutdown(self):
        found = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "raise_signal"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "signal"
            for node in ast.walk(self._source_tree())
        )
        assert found, "signal.raise_signal(...) 呼び出しが見つからない"
