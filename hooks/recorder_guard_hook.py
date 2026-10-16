"""PreToolUse hook（記録役専用）: 片を受け取る前のツール呼び出しを拒否する。

登録は記録役の実行ディレクトリ（run_dir）のsettings.jsonにだけ置く。
プラグイン共通のhooks.jsonには登録しない。run_dirはコマンドライン引数で
受け取る（記録役のcwdは全セッション共通のフォルダのため）。

見張り（hooks/recorder_watch.py）は片を渡すとき、cursor.jsonのpendingに
送り先の記録役のsession_idを書く。pendingがあり、その送り先がこのhookを
呼んだセッションと一致するときだけ通す。起動確認のターン、`DONE -`だけを
返す片なしのターン、立て直し直後で片をまだ再送していないターンは拒否される。

run.jsonが無い（記録役の実行ディレクトリでない）・状態が読めない・想定外の
例外では通す。止めると記録役が一切書けなくなり、通しても今までの挙動に
戻るだけのため。
"""
import json
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.signal_capture import try_capture_guard_block  # noqa: E402
from src.harness import select_harness  # noqa: E402

DENY_REASON = (
    "片を受け取る前はツールを使えない。片の指示（片NNNNを処理せよ）が届くまで"
    "何もせずにターンを終えよ"
)


def shard_received(run_dir: Path, session_id: str | None) -> bool | None:
    """このセッションが片を受け取っているか。記録役の実行ディレクトリでなければNone。"""
    if not (run_dir / "run.json").exists():
        return None
    cursor = json.loads((run_dir / "cursor.json").read_text(encoding="utf-8"))
    pending = cursor.get("pending")
    return bool(pending) and session_id is not None and pending.get("recipient_sid") == session_id


def main(argv: list[str] | None = None) -> None:
    harness = select_harness(hook_event_name="PreToolUse")
    try:
        event = harness.read_hook_input() or {}
        run_dir = Path(argv[0]) if argv else Path(event.get("cwd") or ".")
        received = shard_received(run_dir, event.get("session_id"))
    except Exception:
        received = None
    if received is None or received:
        harness.emit_empty()
        return
    try_capture_guard_block(
        source="hook:recorder_guard",
        summaries=["recorder tool call before shard blocked"],
        detail=f"tool_name={event.get('tool_name')}",
    )
    harness.emit_permission_decision("deny", DENY_REASON)


if __name__ == "__main__":
    main(sys.argv[1:])
