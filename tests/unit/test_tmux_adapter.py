"""tmux.sh ターミナルアダプタのユニットテスト

tmuxコマンドをモックして、spawn/closeの正常系・異常系・特殊文字処理を確認する。
"""
import os
import subprocess
from pathlib import Path

ADAPTER = Path(__file__).resolve().parent.parent.parent / "scripts" / "ow" / "adapters" / "tmux.sh"
MOCK_PANE_ID = "%42"


def _make_mock_tmux(tmp_path: Path, *, has_session: bool = True, capture_file: Path | None = None) -> Path:
    """tmuxをモックするシェルスクリプトを作成する。

    spawn時はMOCK_PANE_IDを返す。close時は何も出力しない。
    capture_fileが指定された場合、受け取った引数を追記する。
    """
    mock_dir = tmp_path / "mock_bin"
    mock_dir.mkdir(exist_ok=True)
    mock = mock_dir / "tmux"

    has_session_exit = "0" if has_session else "1"
    capture_cmd = f'printf "%s\\n" "$*" >> "{capture_file}"' if capture_file else ""

    mock.write_text(
        f'#!/usr/bin/env bash\n'
        f'{capture_cmd}\n'
        f'if [ "$1" = "has-session" ]; then exit {has_session_exit}; fi\n'
        f'if [ "$1" = "new-window" ]; then echo "{MOCK_PANE_ID}"; fi\n'
        f'exit 0\n'
    )
    mock.chmod(0o755)
    return mock_dir


def _run_adapter(args: list[str], tmp_path: Path, **mock_kwargs) -> tuple[subprocess.CompletedProcess, str]:
    """tmux.shをモックtmux環境で実行し、(result, captured_args)を返す。"""
    capture_file = tmp_path / "tmux_args.txt"
    capture_file.write_text("")
    mock_dir = _make_mock_tmux(tmp_path, capture_file=capture_file, **mock_kwargs)

    env = os.environ.copy()
    env["PATH"] = str(mock_dir) + ":" + env["PATH"]

    result = subprocess.run(
        [str(ADAPTER)] + args,
        capture_output=True,
        text=True,
        env=env,
    )
    return result, capture_file.read_text()


class TestTmuxAdapterSpawn:
    def test_spawn_returns_pane_id(self, tmp_path):
        """通常のCWD/WORKER_CMDでspawnが正常終了し、tmux pane IDを返す。"""
        result, _ = _run_adapter(["spawn", "/tmp/normal", "claude"], tmp_path)
        assert result.returncode == 0
        assert MOCK_PANE_ID in result.stdout

    def test_spawn_creates_session_when_missing(self, tmp_path):
        """tmuxセッションが存在しない場合、new-sessionが呼ばれる。"""
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude"], tmp_path, has_session=False
        )
        assert result.returncode == 0
        assert "new-session" in captured

    def test_spawn_skips_session_creation_when_exists(self, tmp_path):
        """tmuxセッションが既に存在する場合、new-sessionが呼ばれない。"""
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude"], tmp_path, has_session=True
        )
        assert result.returncode == 0
        assert "new-session" not in captured

    def test_spawn_cwd_with_spaces(self, tmp_path):
        """CWDにスペースが含まれても正常終了する。"""
        result, _ = _run_adapter(["spawn", "/Users/John Doe/my project", "claude"], tmp_path)
        assert result.returncode == 0

    def test_spawn_worker_cmd_with_single_quotes(self, tmp_path):
        """WORKER_CMDにシングルクォートが含まれても正常終了する。"""
        cmd = "claude --arg 'value with spaces'"
        result, _ = _run_adapter(["spawn", "/tmp/work", cmd], tmp_path)
        assert result.returncode == 0

    def test_spawn_worker_cmd_with_backslash(self, tmp_path):
        """WORKER_CMDにバックスラッシュが含まれても正常終了する。"""
        cmd = r'claude --path /some/path\ with\ spaces'
        result, _ = _run_adapter(["spawn", "/tmp/work", cmd], tmp_path)
        assert result.returncode == 0

    def test_spawn_worker_cmd_with_japanese(self, tmp_path):
        """WORKER_CMDに日本語が含まれても正常終了する。"""
        cmd = 'claude "workerスキルに従って作業を開始して。"'
        result, _ = _run_adapter(["spawn", "/tmp/work", cmd], tmp_path)
        assert result.returncode == 0

    def test_spawn_new_window_called(self, tmp_path):
        """spawnでtmux new-windowが呼ばれる。"""
        result, captured = _run_adapter(["spawn", "/tmp/work", "claude"], tmp_path)
        assert result.returncode == 0
        assert "new-window" in captured


class TestTmuxAdapterClose:
    def test_close_normal_pane_id(self, tmp_path):
        """%42形式のpane IDでcloseが正常終了する。"""
        result, captured = _run_adapter(["close", "%42"], tmp_path)
        assert result.returncode == 0
        assert "kill-pane" in captured

    def test_close_pane_id_passed_to_kill(self, tmp_path):
        """closeでtmux kill-paneにpane IDが渡される。"""
        result, captured = _run_adapter(["close", "%99"], tmp_path)
        assert result.returncode == 0
        assert "%99" in captured

    def test_close_nonexistent_pane_exits_zero(self, tmp_path):
        """存在しないpane IDでもcloseはゼロで終了する（エラーを無視）。"""
        # kill-paneが失敗しても || true で無視するため、exit 0 を期待
        result, _ = _run_adapter(["close", "%999"], tmp_path)
        assert result.returncode == 0


class TestTmuxAdapterErrors:
    def test_unknown_action_exits_nonzero(self, tmp_path):
        """未知のactionはゼロ以外のexit codeとエラーメッセージを返す。"""
        result, _ = _run_adapter(["unknown_action"], tmp_path)
        assert result.returncode != 0
        assert "Unknown action" in result.stderr

    def test_no_args_exits_nonzero(self, tmp_path):
        """引数なし呼び出しはゼロ以外のexit codeとUsageメッセージを返す。"""
        result, _ = _run_adapter([], tmp_path)
        assert result.returncode != 0
        assert "Usage" in result.stderr

    def test_spawn_missing_args_exits_nonzero(self, tmp_path):
        """spawnで引数が足りない場合はゼロ以外のexit codeとUsageメッセージを返す。"""
        result, _ = _run_adapter(["spawn", "/tmp/work"], tmp_path)
        assert result.returncode != 0
        assert "Usage" in result.stderr

    def test_close_missing_args_exits_nonzero(self, tmp_path):
        """closeで引数が足りない場合はゼロ以外のexit codeとUsageメッセージを返す。"""
        result, _ = _run_adapter(["close"], tmp_path)
        assert result.returncode != 0
        assert "Usage" in result.stderr
