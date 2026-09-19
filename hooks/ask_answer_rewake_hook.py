"""PostToolUse hook: add_askの回答待ちを裏で行い、回答されたらセッションを起こす。

add_ask直後にasyncRewakeで起動し、対象askのstatusが変わるまでDBを定期的に
ポーリングする。answered/promoted/dismissedになったらstderrに固定文言
（ask番号とstatusのみ、回答本文・質問文は含めない）を書いてexit 2で返し、
idleセッションを起こす。想定外の例外はすべて握って exit 0 にする（誤って
起こさないことを優先する）。

matcherに加えてtool_nameを二重に確認する（_is_calm_tool + add_ask判定）。
notify=Falseで積まれたaskや、dedupでnotify_wantedが変わらなかったaskは
待たない。サブエージェント発（agent_typeキーの有無で判定。同名の
sanitize_tool_result_hook.py:124と同じ判定で、agent_idは実機検証の結果
常にnullで届き使えないことが確認済み）・無人実行
（CLAUDE_CODE_SESSION_ATTENDED=="0"）のadd_askも待たない。

同じ(session_id, ask_id)の二重待機はロックファイル（flock、LOCK_EX|LOCK_NB）
で防ぐ。ロックファイルは消さない: 消すと、消えた後のinodeに対するロックと
新規作成されたファイルへのロックが並存し得る競合を生む。

DBは読み取り専用（sqlite3 URI mode=ro）で毎回開いて閉じる。既存の読み取り
専用hook（sanitize_tool_result_hook.py等）と同じ流儀で、書き込み系hookが
使うsrc.db.get_connection（sqlite-vecロード・WAL設定を伴う）は使わない。
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
import sys
import time
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.hook_state import HookState
from hooks.hook_transcript import _extract_short_name, _is_calm_tool, _parse_ask_id_from_result
from src.env_compat import env_get
from src.harness import select_harness

DEFAULT_DB_PATH = Path.home() / ".claude" / ".claude-code-memory" / "discussion.db"

POLL_INTERVAL_SECONDS = 15

# 24時間のtimeout指定(hooks.json)が受理されることは実測したが、上限到達時の
# 挙動は未確認なので自分から先に終わる。壁時計(time.time)で開始時刻から
# 測る: monotonicはスリープ中に進まない環境があり、Claude Code側の計時が
# スリープを含む場合にmonotonicで測ると先にkillされうるため。
WAIT_LIMIT_SECONDS = 86400 - 300

_RESOLVED_STATUSES = ("answered", "promoted", "dismissed")

_ANSWERED_TEMPLATE = (
    "CALM: ask #{id} に回答がありました(status: answered)。"
    "get_asks(ids=[{id}], status=null) で回答を読み、回答に沿って作業を続けてください。"
    "読んだら早めに triage_ask で処理済みにしてください"
    "(決定として残す内容なら promote、そうでなければ dismiss)。"
)
_OTHER_RESOLVED_TEMPLATE = (
    "CALM: ask #{id} は回答・処理済みになりました(status: {status})。"
    "get_asks(ids=[{id}], status=null) で内容を読み、作業を続けてください。"
)


def _resolve_db_path() -> str:
    return env_get("CALM_DB_PATH", str(DEFAULT_DB_PATH))


def _extract_ask_id(tool_response) -> int | None:
    if isinstance(tool_response, dict):
        content = tool_response.get("content")
    else:
        content = tool_response
    return _parse_ask_id_from_result(content)


def _acquire_lock(session_id: str, ask_id: int):
    """(session_id, ask_id)専用のロックファイルをexclusive lockして返す。

    既に別プロセスが保持していればNoneを返す。戻り値のfile objectは
    呼び出し側が握り続けること（GCされるとfdが閉じ、flockも解放される）。
    """
    lock_dir = HookState.BASE_DIR / "ask_rewake"
    lock_dir.mkdir(parents=True, exist_ok=True)
    safe_session_id = session_id.replace("/", "_")
    lock_path = lock_dir / f"{safe_session_id}_{ask_id}.lock"
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main(*, sleep=time.sleep, now=time.time) -> int:
    try:
        if os.environ.get("HOOK_STATE_DIR"):
            HookState.BASE_DIR = Path(os.environ["HOOK_STATE_DIR"])

        data = select_harness(hook_event_name="PostToolUse").read_hook_input()

        tool_name = data.get("tool_name", "")
        if not (_is_calm_tool(tool_name) and _extract_short_name(tool_name) == "add_ask"):
            return 0
        if data.get("tool_input", {}).get("notify") is False:
            return 0
        session_id = data.get("session_id")
        if not session_id:
            return 0
        if data.get("agent_type"):
            return 0
        if os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") == "0":
            return 0

        ask_id = _extract_ask_id(data.get("tool_response"))
        if ask_id is None:
            return 0

        lock_fh = _acquire_lock(session_id, ask_id)
        if lock_fh is None:
            return 0
        try:
            return _wait_for_resolution(ask_id, sleep=sleep, now=now)
        finally:
            lock_fh.close()
    except Exception:
        return 0


def _wait_for_resolution(ask_id: int, *, sleep, now) -> int:
    db_path = _resolve_db_path()
    claude_pid = _read_int_env("CLAUDE_PID")
    start = now()

    while True:
        if claude_pid is not None:
            try:
                os.kill(claude_pid, 0)
            except ProcessLookupError:
                return 0
            except PermissionError:
                pass  # 生存はしているが権限が無いだけ

        row, db_ok = _read_ask_row(db_path, ask_id)
        if db_ok:
            if row is None:
                return 0
            status, notify_wanted = row
            if not notify_wanted:
                return 0
            if status in _RESOLVED_STATUSES:
                template = _ANSWERED_TEMPLATE if status == "answered" else _OTHER_RESOLVED_TEMPLATE
                sys.stderr.write(template.format(id=ask_id, status=status) + "\n")
                return 2
            if status != "open":
                return 0
        # db_ok=False（DB未起動・ロック等）は握って次の周期に再試行する

        if now() - start > WAIT_LIMIT_SECONDS:
            return 0
        sleep(POLL_INTERVAL_SECONDS)


def _read_int_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read_ask_row(db_path: str, ask_id: int) -> tuple[tuple | None, bool]:
    """(row, ok)を返す。okはDBに問い合わせできたか（例外なし）。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None, False
    try:
        row = conn.execute(
            "SELECT status, notify_wanted FROM asks WHERE id = ?", (ask_id,)
        ).fetchone()
        return row, True
    except sqlite3.Error:
        return None, False
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
