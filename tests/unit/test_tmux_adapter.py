"""tmux.sh ターミナルアダプタのユニットテスト

tmuxコマンドをモックして、spawn/closeの正常系・異常系・特殊文字処理を確認する。
"""
import os
import subprocess
from pathlib import Path

ADAPTER = Path(__file__).resolve().parent.parent.parent / "scripts" / "ow" / "adapters" / "tmux.sh"
MOCK_PANE_ID = "%42"


def _make_mock_tmux(
    tmp_path: Path,
    *,
    has_session: bool = True,
    capture_file: Path | None = None,
    kill_pane_exit: int = 0,
    target_pane_exists: bool = True,
    existing_worker_panes: str = "",
) -> Path:
    """tmuxをモックするシェルスクリプトを作成する。

    spawn時はMOCK_PANE_IDを返す。close時は何も出力しない。
    capture_fileが指定された場合、受け取った引数を追記する。
    kill_pane_exitが1の場合、kill-paneが失敗するモックになる。

    target_pane_exists=Falseのとき `tmux display` がexit 1を返す（target_pane不在シミュレーション）。
    existing_worker_panesは `tmux list-panes -F "#{pane_id}|#{@ow-worker}"` の擬似出力。
    例: "%5|1\\n%7|" を渡すと既存worker paneが1個ある状態を模擬する（"1"がworkerマーカー、空欄が非worker）。
    """
    mock_dir = tmp_path / "mock_bin"
    mock_dir.mkdir(exist_ok=True)
    mock = mock_dir / "tmux"

    has_session_exit = "0" if has_session else "1"
    capture_cmd = f'printf "%s\\n" "$*" >> "{capture_file}"' if capture_file else ""
    display_exit = "0" if target_pane_exists else "1"
    display_out = '"@1"' if target_pane_exists else '""'

    mock.write_text(
        f'#!/usr/bin/env bash\n'
        f'{capture_cmd}\n'
        f'if [ "$1" = "has-session" ]; then exit {has_session_exit}; fi\n'
        f'if [ "$1" = "new-window" ]; then echo "{MOCK_PANE_ID}"; fi\n'
        f'if [ "$1" = "split-window" ]; then echo "{MOCK_PANE_ID}"; fi\n'
        f'if [ "$1" = "kill-pane" ]; then exit {kill_pane_exit}; fi\n'
        f'if [ "$1" = "display" ]; then echo {display_out}; exit {display_exit}; fi\n'
        f'if [ "$1" = "list-panes" ]; then printf "%b\\n" "{existing_worker_panes}"; fi\n'
        f'exit 0\n'
    )
    mock.chmod(0o755)
    return mock_dir


def _run_adapter(args: list[str], tmp_path: Path, **mock_kwargs) -> tuple["subprocess.CompletedProcess[str]", str]:
    """tmux.shをモックtmux環境で実行し、(result, captured_args)を返す。

    mock_kwargsはそのまま_make_mock_tmuxに渡す（has_session, kill_pane_exitなど）。
    """
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

    def test_close_pane_kill_failure_still_exits_zero(self, tmp_path):
        """kill-paneがexit 1を返しても、|| true によりcloseはゼロで終了する。"""
        # kill_pane_exit=1でkill-paneが常に失敗するモックを使用
        result, _ = _run_adapter(["close", "%999"], tmp_path, kill_pane_exit=1)
        assert result.returncode == 0


class TestTmuxAdapterSplit:
    """target_pane指定時のsplit-window方式のテスト。"""

    def test_spawn_with_target_pane_calls_split_window(self, tmp_path):
        """target_pane指定時はsplit-windowが呼ばれ、new-windowは呼ばれない。"""
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude", "%0"], tmp_path
        )
        assert result.returncode == 0
        assert "split-window" in captured
        assert "new-window" not in captured

    def test_no_target_pane_falls_back_to_new_window(self, tmp_path):
        """target_pane未指定時は従来通りnew-windowで起動し、split-windowは呼ばれない。"""
        result, captured = _run_adapter(["spawn", "/tmp/work", "claude"], tmp_path)
        assert result.returncode == 0
        assert "new-window" in captured
        assert "split-window" not in captured

    def test_first_worker_uses_horizontal_30pct(self, tmp_path):
        """既存worker pane 0個のとき、最初のworkerは -h -l 30% で水平分割される。"""
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude", "%0"],
            tmp_path,
            existing_worker_panes="",
        )
        assert result.returncode == 0
        assert "split-window -h" in captured
        assert "30%" in captured

    def test_subsequent_worker_uses_vertical_split(self, tmp_path):
        """既存worker pane (pane user option @ow-worker=1) があるとき、-v で垂直分割される。"""
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude", "%0"],
            tmp_path,
            existing_worker_panes="%5|1",
        )
        assert result.returncode == 0
        assert "split-window -v" in captured

    def test_split_sets_pane_user_option_marker(self, tmp_path):
        """split-window後にset-option -p で @ow-worker=1 が設定される。"""
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude", "%0"], tmp_path
        )
        assert result.returncode == 0
        assert "set-option -p" in captured
        assert "@ow-worker" in captured
        # 値 "1" が引数列に含まれること (例: "set-option -p -t %42 @ow-worker 1")
        assert "@ow-worker 1" in captured

    def test_split_does_not_use_pane_title_marker(self, tmp_path):
        """旧マーカー方式（select-pane -T "ow-worker"）が呼ばれていないことを保証する。

        claude セッションが pane-title を ANSI escape で動的上書きするため、
        pane-title をマーカーとしては使えない。
        """
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude", "%0"], tmp_path
        )
        assert result.returncode == 0
        assert "select-pane" not in captured

    def test_non_worker_panes_ignored(self, tmp_path):
        """@ow-worker が未設定（空欄）のpaneは既存workerとして扱わず、水平30%分割になる。"""
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude", "%0"],
            tmp_path,
            # list-panes 出力で @ow-worker 列が空（未設定）の2 pane を模擬
            existing_worker_panes="%5|\\n%7|",
        )
        assert result.returncode == 0
        assert "split-window -h" in captured
        assert "30%" in captured

    def test_list_panes_filter_uses_pane_user_option(self, tmp_path):
        """list-panes のフォーマット指定が #{@ow-worker} を参照していることを確認する。

        pane-title ベース判定への退行を防ぐ回帰テスト。
        """
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude", "%0"], tmp_path
        )
        assert result.returncode == 0
        assert "#{@ow-worker}" in captured
        assert "#{pane_title}" not in captured

    def test_target_pane_not_found_exits_nonzero(self, tmp_path):
        """target_paneが存在しないとき、exit 1とstderrエラーメッセージを返す。"""
        result, _ = _run_adapter(
            ["spawn", "/tmp/work", "claude", "%999"],
            tmp_path,
            target_pane_exists=False,
        )
        assert result.returncode != 0
        assert "target_pane not found" in result.stderr


class TestTmuxAdapterThinking:
    """is_thinking=1 のとき思考worker扱いで `tmux new-window` で別タブを開く (D#2601)。"""

    def test_thinking_with_target_pane_uses_new_window(self, tmp_path):
        """target_pane指定 + is_thinking=1 のとき、split-window ではなく new-window が呼ばれる。"""
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude", "%0", "1"], tmp_path
        )
        assert result.returncode == 0
        assert "new-window" in captured
        assert "split-window" not in captured

    def test_thinking_no_target_pane_uses_new_window(self, tmp_path):
        """target_pane未指定 + is_thinking=1 でも new-window 経路（ow-workers セッションに新タブ）になる。"""
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude", "", "1"], tmp_path
        )
        assert result.returncode == 0
        assert "new-window" in captured
        assert "split-window" not in captured

    def test_thinking_sets_window_name_and_pane_marker(self, tmp_path):
        """is_thinking=1 のとき、window名 ow-worker-thinking で new-window され、
        pane user option @ow-worker=1 が set される (通常worker と同じマーカーを共有)。"""
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude", "%0", "1"], tmp_path
        )
        assert result.returncode == 0
        # 思考workerは window-name で識別 (別タブとして可視化される)
        assert "ow-worker-thinking" in captured
        # 通常worker と同じ pane user option を共有 (別window配置のため検出競合は発生しない)
        assert "set-option" in captured
        assert "@ow-worker" in captured

    def test_thinking_zero_target_pane_uses_split(self, tmp_path):
        """is_thinking=0 + target_pane指定なら従来通り split-window 経路 (互換性確認)。"""
        result, captured = _run_adapter(
            ["spawn", "/tmp/work", "claude", "%0", "0"], tmp_path
        )
        assert result.returncode == 0
        assert "split-window" in captured

    def test_thinking_target_pane_not_found_exits_nonzero(self, tmp_path):
        """is_thinking=1 で target_pane が存在しないとき、exit 1 と stderr エラーを返す。"""
        result, _ = _run_adapter(
            ["spawn", "/tmp/work", "claude", "%999", "1"],
            tmp_path,
            target_pane_exists=False,
        )
        assert result.returncode != 0
        assert "target_pane not found" in result.stderr


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
