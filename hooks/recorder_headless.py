"""headlessモードの記録役: 片1件につき`claude -p`を1回呼ぶ。

常駐モードでは、記録役のclaudeが片を待つあいだもセッションとして生きており、
待機の指示を守らずにツールを呼ぶことがあった。headlessモードでは記録役の
claudeは片が来たときにしか存在しない。本プロセスはtmuxペインで動き続け、
常駐モードの見張り（hooks/recorder_watch.py）と同じ`_poll_until_chunk`で片を
切り、片ごとに`claude -p`を起こして片の内容をstdinで渡す。

DONEの受理は常駐モードと同じ`_resolve_pending`を通す（DONEが無ければ1回だけ
再送し、それでも無ければunackedにして先へ進む）。ただし`claude -p`そのものが
失敗した回（終了コード≠0・タイムアウト・出力がJSONでない・is_error）は、
API障害などで片を黙って落とさないよう、この再送枠を使わずに間隔を伸ばしながら
同じ片を出し直す。`LAUNCH_FAILURE_LIMIT`回続けて失敗したときだけunackedにする。
権限拒否は終了コードに出ず`permission_denials`にしか現れないため、空でない回は
DONEがあっても受理しない。

呼び出しごとの結果は`run_dir/headless.log`にJSON Linesで残す。
tmux・flockに依存するためWindowsには未対応。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.hook_state import HookState  # noqa: E402
from hooks.recorder_marker import touch_marker  # noqa: E402
from hooks.recorder_watch import (  # noqa: E402
    POLL_INTERVAL_SECONDS,
    _acquire_lock,
    _collect_unlinked_materials,
    _ensure_activity_id_at_cursor,
    _load_cursor,
    _poll_until_chunk,
    _resolve_pending,
    _write_json_atomic,
)
from src.services.recorder_launcher_service import (  # noqa: E402
    HEADLESS_PROMPT_TEMPLATE,
    RecorderLaunchError,
    append_recorder_sid,
    build_headless_command,
    recorder_cwd,
    resolve_main_transcript,
)

# 1回の呼び出しの上限。目印ファイルの鮮度切れ（15分）より短くする。
RUN_TIMEOUT_SEC = 600

# 起動失敗のあと同じ片を出し直すまでの待ち。失敗のたびに倍にし、上限で止める。
LAUNCH_RETRY_BASE_SEC = 30
LAUNCH_RETRY_MAX_SEC = 240
LAUNCH_FAILURE_LIMIT = 5

# 子のclaudeに渡さない環境変数（記録役に記録役が付く・メインのsession_idを名乗るのを防ぐ）。
_STRIPPED_ENV = ("CALM_RECORDER", "CLAUDE_CODE_SESSION_ID")


def _log(run_dir: Path, record: dict) -> None:
    record = {"at": datetime.now(UTC).isoformat(timespec="seconds"), **record}
    with open(run_dir / "headless.log", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _invoke(run_dir: Path, pending: dict) -> dict:
    """片1件で`claude -p`を1回呼ぶ。

    Returns:
        {"session_id", "launched": bool, "text", "denials", "error", "cost_usd"}。
        launchedがFalseなら起動そのものの失敗で、textは空。
    """
    no = pending["no"]
    sid = str(uuid.uuid4())
    append_recorder_sid(run_dir, sid)
    chunk = (run_dir / "chunks" / f"{no:04d}.md").read_text(encoding="utf-8")
    prompt = HEADLESS_PROMPT_TEMPLATE.format(no=no) + chunk
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
    base = {"session_id": sid, "launched": False, "text": "", "denials": [], "error": None, "cost_usd": None}
    try:
        proc = subprocess.run(
            build_headless_command(_project_root, run_dir, sid),
            input=prompt, capture_output=True, text=True, encoding="utf-8",
            timeout=RUN_TIMEOUT_SEC, cwd=str(recorder_cwd()), env=env,
        )
    except subprocess.TimeoutExpired:
        return {**base, "error": "timeout"}
    except OSError as e:
        return {**base, "error": f"spawn failed: {e}"}
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result = None
    if proc.returncode != 0 or not isinstance(result, dict) or result.get("is_error"):
        detail = result.get("result") if isinstance(result, dict) else proc.stderr
        return {**base, "error": f"exit {proc.returncode}: {str(detail)[:300]}"}
    return {
        **base,
        "launched": True,
        "text": result.get("result") or "",
        "denials": result.get("permission_denials") or [],
        "cost_usd": result.get("total_cost_usd"),
    }


def _sleep_keeping_marker(main_sid: str, seconds: float, sleep) -> None:
    """目印ファイルの鮮度を保ちながら待つ（待つ間に記録役が外れた扱いにならないように）。"""
    remaining = seconds
    while remaining > 0:
        touch_marker(main_sid)
        step = min(POLL_INTERVAL_SECONDS, remaining)
        sleep(step)
        remaining -= step


def _own_transcript(session_id: str) -> Path | None:
    """この呼び出しの記録役自身のtranscript（~/.claude/projects/*/<sid>.jsonl）。"""
    try:
        return resolve_main_transcript(session_id, None)
    except RecorderLaunchError:
        return None


def run(run_dir: Path, *, sleep=time.sleep, now=time.time, invoke=_invoke) -> int:
    run_data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    main_sid = run_data["main_sid"]
    main_transcript = Path(run_data["main_transcript"]).expanduser()
    cursor_path = run_dir / "cursor.json"
    cursor = _load_cursor(cursor_path)
    _ensure_activity_id_at_cursor(cursor, main_transcript, cursor_path)

    while True:
        if cursor.get("pending") is None:
            outcome = _poll_until_chunk(
                run_dir, cursor, cursor_path, run_data, None,
                sleep=sleep, now=now, wait_limit=None, claude_pid=None,
            )
            if outcome != "chunk":
                return 0

        pending = cursor["pending"]
        touch_marker(main_sid)
        result = invoke(run_dir, pending)
        touch_marker(main_sid)
        own = _own_transcript(result["session_id"])
        if own is not None:
            # 常駐モードと同じキーで、この片を処理したtranscriptの位置を渡す。
            # 呼び出しごとに別セッションなので位置は常に先頭。
            pending["transcript_path"] = str(own)
            pending["transcript_offset"] = 0
            if pending.get("end_activity_id") is None:
                # 失敗した回の書き込みも拾うため、受理の判定を待たずに集める。
                cursor.setdefault("unlinked_materials", []).extend(_collect_unlinked_materials(own, 0))

        if not result["launched"]:
            failures = pending.get("launch_failures", 0) + 1
            pending["launch_failures"] = failures
            _log(run_dir, {"no": pending["no"], "session_id": result["session_id"],
                           "outcome": "launch_failed", "error": result["error"]})
            if failures < LAUNCH_FAILURE_LIMIT:
                _write_json_atomic(cursor_path, cursor)
                wait = min(LAUNCH_RETRY_BASE_SEC * 2 ** (failures - 1), LAUNCH_RETRY_MAX_SEC)
                _sleep_keeping_marker(main_sid, wait, sleep)
                continue
            # 出し直しを使い切った。DONE待ちの再送もせずにunackedにする。
            pending["retries"] = 1
            last_msg = ""
        else:
            last_msg = "" if result["denials"] else result["text"]

        outcome = _resolve_pending(cursor, last_msg)
        _write_json_atomic(cursor_path, cursor)
        if result["launched"]:
            _log(run_dir, {"no": pending["no"], "session_id": result["session_id"], "outcome": outcome,
                           "denials": result["denials"], "cost_usd": result["cost_usd"]})


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        return 0
    argv = sys.argv[1:] if argv is None else argv
    if os.environ.get("HOOK_STATE_DIR"):
        HookState.BASE_DIR = Path(os.environ["HOOK_STATE_DIR"])
    run_dir = Path(argv[0])
    if not (run_dir / "run.json").exists():
        return 1
    lock_fh = _acquire_lock(run_dir)
    if lock_fh is None:
        return 0
    try:
        return run(run_dir)
    except Exception:
        # ペインごと消えるとstderrは残らないため、手がかりをrun_dirに書く。
        # 本プロセスが落ちると目印のpidが死に、次の人の発話で起こし直される。
        _log(run_dir, {"outcome": "crashed", "error": traceback.format_exc()})
        return 1
    finally:
        lock_fh.close()


if __name__ == "__main__":
    sys.exit(main())
