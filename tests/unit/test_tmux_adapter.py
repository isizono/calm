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
    display_switch_at_kill: int | None = None,
) -> Path:
    """tmuxをモックするシェルスクリプトを作成する。

    spawn時はMOCK_PANE_IDを返す。close時はpane_pid相当のダミー値を返す。
    capture_fileが指定された場合、受け取った引数を追記する。
    kill_pane_exitが1の場合、kill-paneが失敗するモックになる。

    target_pane_exists=Falseのとき `tmux display` がexit 1を返す（target_pane不在シミュレーション）。
    display_switch_at_kill=N のとき、N回目以降の kill-pane 呼び出し後に display が
    「不在」(exit 1) に切り替わる（SIGKILL fallback シナリオの再現用）。

    existing_worker_panesは `tmux list-panes -F "#{pane_id}|#{@ow-worker}"` の擬似出力。
    例: "%5|1\\n%7|" を渡すと既存worker paneが1個ある状態を模擬する（"1"がworkerマーカー、空欄が非worker）。
    """
    mock_dir = tmp_path / "mock_bin"
    mock_dir.mkdir(exist_ok=True)
    mock = mock_dir / "tmux"

    has_session_exit = "0" if has_session else "1"
    capture_cmd = f'printf "%s\\n" "$*" >> "{capture_file}"' if capture_file else ""
    display_state_file = tmp_path / "tmux_display_state"
    kill_counter_file = tmp_path / "tmux_kill_counter"
    display_state_file.write_text("1" if target_pane_exists else "0")
    kill_counter_file.write_text("0")
    switch_at = "" if display_switch_at_kill is None else str(display_switch_at_kill)

    mock.write_text(
        f'#!/usr/bin/env bash\n'
        f'{capture_cmd}\n'
        f'if [ "$1" = "has-session" ]; then exit {has_session_exit}; fi\n'
        f'if [ "$1" = "new-window" ]; then echo "{MOCK_PANE_ID}"; fi\n'
        f'if [ "$1" = "split-window" ]; then echo "{MOCK_PANE_ID}"; fi\n'
        f'if [ "$1" = "kill-pane" ]; then\n'
        f'  cnt=$(cat "{kill_counter_file}" 2>/dev/null || echo 0)\n'
        f'  cnt=$((cnt + 1))\n'
        f'  echo "$cnt" > "{kill_counter_file}"\n'
        f'  switch_at="{switch_at}"\n'
        f'  if [ -n "$switch_at" ] && [ "$cnt" -ge "$switch_at" ]; then\n'
        f'    echo "0" > "{display_state_file}"\n'
        f'  fi\n'
        f'  exit {kill_pane_exit}\n'
        f'fi\n'
        f'if [ "$1" = "display" ]; then\n'
        f'  state=$(cat "{display_state_file}" 2>/dev/null || echo "1")\n'
        f'  if [ "$state" = "0" ]; then exit 1; fi\n'
        f'  echo "12345"\n'
        f'  exit 0\n'
        f'fi\n'
        f'if [ "$1" = "list-panes" ]; then printf "%b\\n" "{existing_worker_panes}"; fi\n'
        f'exit 0\n'
    )
    mock.chmod(0o755)
    return mock_dir


def _run_adapter(
    args: list[str],
    tmp_path: Path,
    extra_env: dict | None = None,
    **mock_kwargs,
) -> tuple["subprocess.CompletedProcess[str]", str]:
    """tmux.shをモックtmux環境で実行し、(result, captured_args)を返す。

    mock_kwargsはそのまま_make_mock_tmuxに渡す（has_session, kill_pane_exitなど）。
    extra_env は追加環境変数（OW_CLOSE_FALLBACK_ITER 等）。
    """
    capture_file = tmp_path / "tmux_args.txt"
    capture_file.write_text("")
    mock_dir = _make_mock_tmux(tmp_path, capture_file=capture_file, **mock_kwargs)

    env = os.environ.copy()
    env["PATH"] = str(mock_dir) + ":" + env["PATH"]
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [str(ADAPTER)] + args,
        capture_output=True,
        text=True,
        env=env,
    )
    return result, capture_file.read_text()


# テスト全体で close fallback の sleep を 0 にして高速化
CLOSE_FAST_ENV = {"OW_CLOSE_FALLBACK_INTERVAL": "0", "OW_CLOSE_FALLBACK_ITER": "2"}


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
    def test_close_already_absent_outputs_closed(self, tmp_path):
        """既に pane が存在しない term_ref を渡すと stdout に 'closed' を出して exit 0。"""
        result, captured = _run_adapter(
            ["close", "%999"], tmp_path, target_pane_exists=False
        )
        assert result.returncode == 0
        assert result.stdout.strip().splitlines()[-1] == "closed"
        # 既に不在のため kill-pane は呼ばれない
        assert "kill-pane" not in captured

    def test_close_kill_pane_succeeds_outputs_closed(self, tmp_path):
        """kill-pane 後に pane が消えれば stdout 'closed' を出して exit 0。"""
        # display_switch_at_kill=1: 1回目の kill-pane 後に display が不在に切り替わる
        result, captured = _run_adapter(
            ["close", "%42"],
            tmp_path,
            display_switch_at_kill=1,
            extra_env=CLOSE_FAST_ENV,
        )
        assert result.returncode == 0
        assert result.stdout.strip().splitlines()[-1] == "closed"
        assert "kill-pane" in captured

    def test_close_passes_pane_id_to_kill(self, tmp_path):
        """closeでtmux kill-paneにpane IDが渡される。"""
        result, captured = _run_adapter(
            ["close", "%99"],
            tmp_path,
            display_switch_at_kill=1,
            extra_env=CLOSE_FAST_ENV,
        )
        assert result.returncode == 0
        assert "%99" in captured


class TestTmuxAdapterCloseSigkillFallback:
    """SIGKILL fallback シナリオ — kill-pane が効かなかったときに 2 回目で消える"""

    def test_close_sigkill_fallback_outputs_killed(self, tmp_path):
        """1回目の kill-pane 後も残存し、SIGKILL fallback (2回目の kill-pane) で消える → 'killed'。"""
        result, captured = _run_adapter(
            ["close", "%42"],
            tmp_path,
            display_switch_at_kill=2,
            extra_env=CLOSE_FAST_ENV,
        )
        assert result.returncode == 0
        assert result.stdout.strip().splitlines()[-1] == "killed"
        # kill-pane が2回呼ばれている（1回目: SIGHUP, 2回目: SIGKILL fallback）
        assert captured.count("kill-pane") == 2

    def test_close_pane_persists_outputs_failed(self, tmp_path):
        """SIGKILL fallback 後も pane が残るケースは 'failed' を stdout/stderr に出して exit 1。"""
        # display_switch_at_kill=None → 常に存在する状態
        result, _ = _run_adapter(
            ["close", "%42"],
            tmp_path,
            extra_env=CLOSE_FAST_ENV,
        )
        assert result.returncode == 1
        assert result.stdout.strip().splitlines()[-1] == "failed"
        assert "failed" in result.stderr


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
