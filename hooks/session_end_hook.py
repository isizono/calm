"""SessionEnd hook: sync-memory未実行セッションのauto-sync起動

処理フロー:
1. stdin読み込み → transcript_path抽出
2. transcriptファイルの存在確認
3. transcript解析（マーカーチェック + user_message_count）を1パスで実行
4. スキップ条件に該当しなければ claude -p をバックグラウンド起動

出力: {"decision": "approve"}（常にapprove）
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.hook_transcript import is_user_message
from hooks.signal_capture import try_capture_signal

_SYNC_MARKER = "claude-code-memory:sync-memory"
_LOG_FILE = Path("/tmp/claude-session-end.log")
_MIN_USER_MESSAGES = 2


def _approve() -> None:
    print(json.dumps({"decision": "approve"}))


def _log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_LOG_FILE, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def _analyze_transcript(transcript_path: Path) -> tuple[bool, int]:
    """transcriptを1パスで解析し、(has_sync_marker, user_message_count)を返す。"""
    has_marker = False
    user_count = 0
    try:
        with open(transcript_path) as f:
            for line in f:
                if _SYNC_MARKER in line:
                    has_marker = True
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if is_user_message(entry):
                        user_count += 1
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        _log(f"Failed to read transcript: {e}")
    return has_marker, user_count


def _launch_auto_sync(transcript_path: Path, script_dir: Path) -> int:
    """claude -pをバックグラウンド起動してauto-syncを実行する。

    Returns:
        起動したプロセスのPID
    """
    prompt_path = script_dir / "auto_sync_prompt.txt"
    system_prompt = prompt_path.read_text()

    # CLAUDECODE環境変数を除外（ネスト検出による無限ループ防止）
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    # transcriptをstdinとして渡す（bash版の cat $TRANSCRIPT_PATH | claude -p に相当）
    stdin_file = open(transcript_path)  # noqa: SIM115
    log_file = open(_LOG_FILE, "a")  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            [
                "claude", "-p",
                "--model", "sonnet",
                "--permission-mode", "dontAsk",
                "--system-prompt", system_prompt,
                "以下はClaude Codeセッションのtranscriptです。sync-memory手順に従って解析・記録してください。",
            ],
            stdin=stdin_file,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            env=env,
            start_new_session=True,
        )
    finally:
        # 子プロセスがFDをコピー済みのため、親側は即クローズ
        # Popen失敗時もリークしないようfinallyで保証
        stdin_file.close()
        log_file.close()
    return proc.pid


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
        transcript_path_str = data.get("transcript_path", "")

        script_dir = Path(__file__).resolve().parent

        _log(f"SessionEnd hook started. transcript_path={transcript_path_str}")

        # transcriptファイルの存在確認
        if not transcript_path_str:
            _log("transcript_path is empty. Skipping.")
            _approve()
            return

        transcript_path = Path(transcript_path_str)
        if not transcript_path.exists():
            _log("transcript file does not exist. Skipping.")
            _approve()
            return

        # transcript解析（1パス）
        has_marker, user_count = _analyze_transcript(transcript_path)

        if has_marker:
            _log("sync-memory already executed. Skipping auto-sync.")
            _approve()
            return

        if user_count < _MIN_USER_MESSAGES:
            _log(f"One-liner session (user_message_count={user_count}). Skipping auto-sync.")
            _approve()
            return

        _log("sync-memory not found in transcript. Launching claude -p for auto-sync.")

        pid = _launch_auto_sync(transcript_path, script_dir)

        _log(f"claude -p launched in background (pid={pid}).")

        _approve()

    except Exception as e:
        print(f"session_end_hook.py error: {e}", file=sys.stderr)
        try_capture_signal(kind="machine_error", source="hook:session_end", summary=str(e)[:200])
        _approve()


if __name__ == "__main__":
    main()
