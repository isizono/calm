"""器のhook: 観測台帳への書き込み専用。

hooks/hooks.json の器の5エントリ（SessionStart・UserPromptSubmit・PostToolUse・
PostToolUseFailure・Stop）を、標準入力の hook_event_name で分けて処理する1本の
スクリプト。この分割で書くのは観測（utterance・speaker・reply・tool・
tool_overflow・tool_fail・boundary）だけで、配達・踏み跡・書き込みの結び付け・
撤回の宣言・訂正の合図は書かない。どのイベントも標準出力には何も書かない
（Claude Codeへの応答を返さない）。

DBへの書き込みは hooks/citation_event_log.py の形に揃え、src.db を経由せず
sqlite3 を直接使う（起動コストを抑えるため）。例外はすべてfail-open
（止めない・差し戻さない）とし、失敗は ~/.cc-memory/logs/feedback_hook.jsonl に
1行残す。器のテーブルが無ければ何もしない。停止スイッチが読めないときは
止める側（'off'）と同じにふるまう。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.env_compat import env_get
from src.services.feedback_rules import (
    TOOL_CALLS_PER_TURN_MAX,
    TOOL_FAIL_MAX_CHARS,
    TOOL_SUMMARY_MAX_CHARS,
    bodies_match,
    compute_flag,
    transcript_body,
)

DEFAULT_DB_PATH = Path.home() / ".claude" / ".claude-code-memory" / "discussion.db"
DEFAULT_LOG_PATH = Path.home() / ".cc-memory" / "logs" / "feedback_hook.jsonl"

CONNECT_TIMEOUT = 2.0
BUSY_TIMEOUT_MS = 2000

# transcriptのユーザー行として扱うtype値（"human"は旧形式transcriptの別名）。
_USER_ROW_TYPES = ("user", "human")

# 観測用の仕掛け: イベントごとに標準入力のキー名の一覧（値は書かない）をログへ
# 残す。恒久機構ではない。
STDIN_KEY_PROBE_ENABLED = True


def _resolve_db_path() -> str:
    return env_get("CALM_DB_PATH", str(DEFAULT_DB_PATH))


def _resolve_log_path() -> Path:
    return Path(env_get("CALM_FEEDBACK_LOG_PATH", str(DEFAULT_LOG_PATH)))


def _log(payload: dict) -> None:
    try:
        log_path = _resolve_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), **payload}
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # ログ自体の失敗もfail-open


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=CONNECT_TIMEOUT)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def read_mode(db_path: str) -> str:
    """器の mode を返す。読めない理由が何であれ 'off' を返す。"""
    try:
        conn = sqlite3.connect(db_path, timeout=CONNECT_TIMEOUT)
    except Exception as e:
        _log({"at": "read_mode", "why": "connect_failed", "err": repr(e)})
        return "off"
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        row = conn.execute("SELECT mode FROM feedback_meta WHERE id = 1").fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            # マイグレーション未適用。器が入る前の正常な状態なので記録しない。
            return "off"
        _log({"at": "read_mode", "why": "query_failed", "err": repr(e)})
        return "off"
    except Exception as e:
        _log({"at": "read_mode", "why": "query_failed", "err": repr(e)})
        return "off"
    finally:
        conn.close()
    if row is None or row[0] not in ("off", "observe", "on"):
        _log({"at": "read_mode", "why": "bad_value", "value": None if row is None else row[0]})
        return "off"
    return row[0]


# ---------------------------------------------------------------------------
# transcript読み取り（stdlibのみ、src.harnessには依存しない）
# ---------------------------------------------------------------------------


def _parse_jsonl_bytes(data: bytes) -> list[dict]:
    out: list[dict] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _read_transcript_lines(path: str | None) -> list[dict]:
    """transcriptファイル全体を読み、パースできた行のdictを返す。

    ファイル不在・パス無しは空リスト。解析できない行は読み飛ばす。
    """
    if not path:
        return []
    p = Path(path).expanduser()
    if not p.exists():
        return []
    try:
        return _parse_jsonl_bytes(p.read_bytes())
    except OSError:
        return []


def _read_transcript_from_offset(path: str | None, offset: int) -> tuple[list[dict], int]:
    """transcriptをバイトオフセットから差分読みする。

    offsetがファイルサイズを超えている場合は0にリセットして全読みする。
    戻り値は (新規エントリ, 次回読み出し用の新オフセット)。
    """
    if not path:
        return [], offset
    p = Path(path).expanduser()
    if not p.exists():
        return [], offset
    file_size = p.stat().st_size
    if offset > file_size:
        offset = 0
    with p.open("rb") as f:
        f.seek(offset)
        data = f.read()
        new_offset = offset + len(data)
    return _parse_jsonl_bytes(data), new_offset


def _select_single_user_match(rows: list[dict], prompt_id: str, prompt: str) -> dict | None:
    """prompt_idと本文が一致するユーザー行を1行に絞る。0行・2行以上ならNone。"""
    matches = [
        row
        for row in rows
        if row.get("type") in _USER_ROW_TYPES
        and row.get("promptId") == prompt_id
        and bodies_match(prompt, row.get("message", {}).get("content"))
    ]
    if len(matches) != 1:
        return None
    return matches[0]


# 起動の系列が分かる CLAUDE_CODE_* 環境変数。トークン・鍵らしき名前のもの
# （*_TOKEN・*_SOCKET等）は除く。
_CLAUDE_LINEAGE_ENV_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION")


def _hook_context(data: dict) -> dict:
    """観測用の生値。transcriptの7キーの外にある材料で、判定には一切使わない。

    取れなかった値はキーを落とさずnullのまま残す。stdin/stdoutはhookが標準入力
    でJSONを受け取る構造上、常にisatty()=Falseになりうる（パイプ経由のため）。
    それでも値そのものは観測のため残す。
    """
    context = {
        "cwd": data.get("cwd"),
        "transcript_path": data.get("transcript_path"),
        "agent_type": data.get("agent_type"),
        "term": os.environ.get("TERM"),
        "term_program": os.environ.get("TERM_PROGRAM"),
        "tmux": "TMUX" in os.environ,
        "sty": "STY" in os.environ,
        "ssh_tty": "SSH_TTY" in os.environ,
        "stdin_isatty": sys.stdin.isatty(),
        "stdout_isatty": sys.stdout.isatty(),
    }
    for name in _CLAUDE_LINEAGE_ENV_VARS:
        context[name.lower()] = os.environ.get(name)
    return context


def _speaker_json(row: dict, data: dict) -> str:
    return json.dumps(
        {
            "promptSource": row.get("promptSource"),
            "turnOrigin": row.get("turnOrigin"),
            "entrypoint": row.get("entrypoint"),
            "userType": row.get("userType"),
            "isMeta": row.get("isMeta"),
            "isSidechain": row.get("isSidechain"),
            "origin": row.get("origin"),
            "hook_context": _hook_context(data),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# イベントごとの処理
# ---------------------------------------------------------------------------


def _handle_session_start(conn: sqlite3.Connection, data: dict) -> None:
    session_id = data.get("session_id")
    if not session_id:
        return
    conn.execute(
        "INSERT INTO obs_events (session_id, agent_id, kind) VALUES (?, ?, 'boundary')",
        (session_id, data.get("agent_id")),
    )


def _handle_user_prompt_submit(conn: sqlite3.Connection, data: dict) -> None:
    session_id = data.get("session_id")
    prompt = data.get("prompt")
    if not session_id or prompt is None:
        return
    prompt_id = data.get("prompt_id")
    agent_id = data.get("agent_id")

    flag = compute_flag(prompt)
    cur = conn.execute(
        "INSERT INTO obs_events (session_id, prompt_id, agent_id, kind, flag, text) "
        "VALUES (?, ?, ?, 'utterance', ?, ?)",
        (session_id, prompt_id, agent_id, flag, prompt),
    )
    utterance_id = cur.lastrowid

    if not prompt_id:
        return
    rows = _read_transcript_lines(data.get("transcript_path"))
    matched = _select_single_user_match(rows, prompt_id, prompt)
    if matched is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO obs_events (session_id, kind, text, ref_id) "
        "VALUES (?, 'speaker', ?, ?)",
        (session_id, _speaker_json(matched, data), utterance_id),
    )


def _summarize_tool_input(tool_input: object) -> str:
    try:
        summary = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        summary = str(tool_input)
    return summary[:TOOL_SUMMARY_MAX_CHARS]


def _handle_post_tool_use(conn: sqlite3.Connection, data: dict) -> None:
    session_id = data.get("session_id")
    tool_name = data.get("tool_name")
    if not session_id or not tool_name:
        return
    prompt_id = data.get("prompt_id")
    agent_id = data.get("agent_id")

    count = conn.execute(
        "SELECT COUNT(*) FROM obs_events WHERE session_id = ? AND prompt_id IS ? AND kind = 'tool'",
        (session_id, prompt_id),
    ).fetchone()[0]

    if count < TOOL_CALLS_PER_TURN_MAX:
        summary = _summarize_tool_input(data.get("tool_input"))
        conn.execute(
            "INSERT INTO obs_events (session_id, prompt_id, agent_id, kind, tool_name, tool_use_id, text) "
            "VALUES (?, ?, ?, 'tool', ?, ?, ?)",
            (session_id, prompt_id, agent_id, tool_name, data.get("tool_use_id"), summary),
        )
        return

    overflow_exists = conn.execute(
        "SELECT 1 FROM obs_events WHERE session_id = ? AND prompt_id IS ? AND kind = 'tool_overflow' LIMIT 1",
        (session_id, prompt_id),
    ).fetchone()
    if overflow_exists is None:
        conn.execute(
            "INSERT INTO obs_events (session_id, prompt_id, agent_id, kind, tool_name, tool_use_id) "
            "VALUES (?, ?, ?, 'tool_overflow', ?, ?)",
            (session_id, prompt_id, agent_id, tool_name, data.get("tool_use_id")),
        )


def _handle_post_tool_use_failure(conn: sqlite3.Connection, data: dict) -> None:
    session_id = data.get("session_id")
    tool_name = data.get("tool_name")
    if not session_id or not tool_name:
        return
    error = data.get("error")
    text = str(error)[:TOOL_FAIL_MAX_CHARS] if error is not None else None
    conn.execute(
        "INSERT INTO obs_events (session_id, prompt_id, agent_id, kind, tool_name, tool_use_id, text) "
        "VALUES (?, ?, ?, 'tool_fail', ?, ?, ?)",
        (
            session_id,
            data.get("prompt_id"),
            data.get("agent_id"),
            tool_name,
            data.get("tool_use_id"),
            text,
        ),
    )


def _handle_stop(conn: sqlite3.Connection, data: dict) -> None:
    session_id = data.get("session_id")
    if not session_id:
        return
    transcript_path = data.get("transcript_path")

    cursor_row = conn.execute(
        "SELECT byte_offset FROM feedback_cursor WHERE session_id = ?", (session_id,)
    ).fetchone()
    offset = cursor_row[0] if cursor_row else 0
    new_rows, new_offset = _read_transcript_from_offset(transcript_path, offset)

    user_rows: list[dict] = []
    last_prompt_id: str | None = None
    wrote_reply = False
    for row in new_rows:
        row_type = row.get("type")
        if row_type in _USER_ROW_TYPES:
            user_rows.append(row)
            if row.get("promptId"):
                last_prompt_id = row.get("promptId")
            continue
        if row_type == "assistant":
            text = transcript_body(row.get("message", {}).get("content")).strip()
            if not text:
                continue
            prompt_id = row.get("promptId") or last_prompt_id
            conn.execute(
                "INSERT OR IGNORE INTO obs_events (session_id, prompt_id, kind, text, src_uuid) "
                "VALUES (?, ?, 'reply', ?, ?)",
                (session_id, prompt_id, text, row.get("uuid")),
            )
            wrote_reply = True

    if not wrote_reply:
        last_assistant_message = data.get("last_assistant_message")
        if last_assistant_message:
            conn.execute(
                "INSERT OR IGNORE INTO obs_events (session_id, prompt_id, kind, text, src_uuid) "
                "VALUES (?, ?, 'reply', ?, NULL)",
                (session_id, last_prompt_id, last_assistant_message),
            )

    if user_rows:
        pending = conn.execute(
            "SELECT id, prompt_id, text FROM obs_events "
            "WHERE session_id = ? AND kind = 'utterance' "
            "AND id NOT IN (SELECT ref_id FROM obs_events WHERE kind = 'speaker' AND ref_id IS NOT NULL)",
            (session_id,),
        ).fetchall()
        for utterance_id, prompt_id, text in pending:
            if not prompt_id:
                continue
            matched = _select_single_user_match(user_rows, prompt_id, text)
            if matched is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO obs_events (session_id, kind, text, ref_id) "
                "VALUES (?, 'speaker', ?, ?)",
                (session_id, _speaker_json(matched, data), utterance_id),
            )

    conn.execute(
        "INSERT INTO feedback_cursor (session_id, byte_offset) VALUES (?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET byte_offset = excluded.byte_offset",
        (session_id, new_offset),
    )


_HANDLERS = {
    "SessionStart": _handle_session_start,
    "UserPromptSubmit": _handle_user_prompt_submit,
    "PostToolUse": _handle_post_tool_use,
    "PostToolUseFailure": _handle_post_tool_use_failure,
    "Stop": _handle_stop,
}


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception as e:
        _log({"at": "read_input", "err": repr(e)})
        return

    event = data.get("hook_event_name")
    if STDIN_KEY_PROBE_ENABLED:
        _log({"at": "stdin_keys", "event": event, "keys": sorted(data.keys())})

    handler = _HANDLERS.get(event)
    if handler is None:
        return

    db_path = _resolve_db_path()
    mode = read_mode(db_path)
    if mode == "off":
        return

    try:
        conn = _connect(db_path)
    except Exception as e:
        _log({"at": "connect", "event": event, "err": repr(e)})
        return
    try:
        try:
            handler(conn, data)
            conn.commit()
        except Exception as e:
            conn.rollback()
            _log({"at": "handle", "event": event, "err": repr(e)})
    finally:
        conn.close()
    # 標準出力には何も書かない（Claude Codeへの応答を返さない）


if __name__ == "__main__":
    main()
