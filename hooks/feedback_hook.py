"""フィードバック機構のhook本体（1ファイルでUserPromptSubmit・PostToolUseFailure・
PreToolUseの3イベントを処理する）。

標準入力JSONの`hook_event_name`で処理を振り分ける。DB接続失敗・feedback_meta未作成・
mode値が不正のいずれも mode='off' 相当としてfail-open（何も出さず終了、blockも
効かない）。他のDB書き込みhook（citation_event_log.py等）と同じ方針で、起動コストを
抑えるため src.db を経由せずsqlite3を直接使う。

例外処理方針: main()全体をtry/exceptで囲み、想定外の例外はすべてharness.emit_empty()
で握って終了する（hook自体の不具合で他のtool実行を止めないため）。保存済みエントリの
条件JSON評価で例外（壊れた正規表現等）が出た場合は、そのエントリだけ評価をスキップし
他のエントリの評価は継続する（_matches内で握る）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.env_compat import env_get  # noqa: E402
from src.harness import select_harness  # noqa: E402
from src.services.feedback_rules import evaluate_condition  # noqa: E402

DEFAULT_DB_PATH = Path.home() / ".claude" / ".claude-code-memory" / "discussion.db"

MAX_SHOWN = 3
UTTERANCE_BUDGET_CHARS = 1000
TOOL_FAIL_BUDGET_CHARS = 600
PRE_TOOL_BUDGET_CHARS = 600

BOOTSTRAP_MESSAGE = (
    "躓いた・エラーに遭遇したら、write_feedback_entryで知見を書くか、"
    "既存エントリにadd_feedback_noteでノートを足してください。"
)


def _resolve_db_path() -> str:
    return env_get("CALM_DB_PATH", str(DEFAULT_DB_PATH))


def _connect() -> Optional[sqlite3.Connection]:
    """DB接続を試みる。失敗したらNone（呼び出し側でfail-open扱いする）。"""
    try:
        conn = sqlite3.connect(_resolve_db_path(), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    except sqlite3.Error:
        return None


def _mode_on(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT mode FROM feedback_meta WHERE id = 1").fetchone()
    except sqlite3.Error:
        return False
    return row is not None and row["mode"] == "on"


def _wrap(body: str) -> str:
    return f"<system-reminder>{body}</system-reminder>"


def _fetch_entries(conn: sqlite3.Connection, *, strength: str, timing: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM feedback_entries WHERE strength = ? AND timing = ? AND deleted_at IS NULL ORDER BY id",
        (strength, timing),
    ).fetchall()


def _matches(entry: sqlite3.Row, **kwargs) -> bool:
    """condition_jsonを評価する。壊れたJSON・不正な正規表現等はこのエントリだけ

    スキップする（Falseとして扱い、他エントリの評価は継続する）。
    """
    try:
        condition = json.loads(entry["condition_json"])
        return evaluate_condition(condition, **kwargs)
    except Exception as e:
        print(f"feedback_hook.py: skip entry {entry['id']} ({entry['name']}): {e}", file=sys.stderr)
        return False


def _turn_marked(conn: sqlite3.Connection, session_id: str, prompt_id: str, entry_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM feedback_turn_marks WHERE session_id = ? AND prompt_id = ? AND entry_id = ?",
        (session_id, prompt_id, entry_id),
    ).fetchone()
    return row is not None


def _select_shown(
    candidates: list[sqlite3.Row], *, budget_chars: int, render
) -> list[tuple[sqlite3.Row, str]]:
    """件数上限(MAX_SHOWN)・字数上限(budget_chars)に収まる分だけ選ぶ。

    candidatesは既に「区切り重複で除外済み」の一覧を渡すこと。1件も入らない字数の
    エントリに当たっても、それより後の(短い)エントリを拾いには行かない
    （件数上限3件と同様、先頭から詰めるだけの単純な規則）。
    """
    shown: list[tuple[sqlite3.Row, str]] = []
    total_len = 0
    for entry in candidates:
        if len(shown) >= MAX_SHOWN:
            break
        text = render(entry)
        if total_len + len(text) > budget_chars:
            break
        shown.append((entry, text))
        total_len += len(text)
    return shown


def _deliver(conn: sqlite3.Connection, session_id: str, prompt_id: str, shown: list[tuple[sqlite3.Row, str]]) -> None:
    for entry, _text in shown:
        conn.execute(
            "UPDATE feedback_entries SET delivered_count = delivered_count + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (entry["id"],),
        )
        conn.execute(
            "INSERT OR IGNORE INTO feedback_turn_marks (session_id, prompt_id, entry_id) VALUES (?, ?, ?)",
            (session_id, prompt_id, entry["id"]),
        )


def _notify_render(entry: sqlite3.Row) -> str:
    return f"- {entry['body']}({entry['delivered_count'] + 1}回目)"


# ---------------------------------------------------------------------------
# UserPromptSubmit
# ---------------------------------------------------------------------------


def _handle_user_prompt_submit(harness, event: dict) -> None:
    session_id = event.get("session_id") or ""
    if not session_id:
        harness.emit_empty()
        return
    prompt = event.get("prompt")
    if not isinstance(prompt, str):
        prompt = ""
    prompt_id = event.get("prompt_id") or ""

    conn = _connect()
    if conn is None:
        harness.emit_empty()
        return
    try:
        if not _mode_on(conn):
            harness.emit_empty()
            return

        entries = _fetch_entries(conn, strength="notify", timing="utterance")
        candidates = [
            e for e in entries
            if not _turn_marked(conn, session_id, prompt_id, e["id"])
            and _matches(e, timing="utterance", prompt_text=prompt)
        ]
        shown = _select_shown(candidates, budget_chars=UTTERANCE_BUDGET_CHARS, render=_notify_render)
        if not shown:
            harness.emit_empty()
            return

        _deliver(conn, session_id, prompt_id, shown)
        conn.commit()
        body = "過去の躓きから学んだ注意点:\n" + "\n".join(text for _, text in shown)
        harness.emit_additional_context(_wrap(body))
    except sqlite3.Error:
        harness.emit_empty()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PostToolUseFailure
# ---------------------------------------------------------------------------


def _extract_error_text(event: dict) -> str:
    """ツール失敗のエラー本文を取り出す。

    フィールド名は複数候補(tool_error/error/tool_response)を順に試す。
    """
    for key in ("tool_error", "error"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    tool_response = event.get("tool_response")
    if isinstance(tool_response, dict):
        for key in ("error", "message"):
            value = tool_response.get(key)
            if isinstance(value, str) and value:
                return value
        return json.dumps(tool_response, ensure_ascii=False)
    if isinstance(tool_response, str):
        return tool_response
    return ""


def _handle_post_tool_use_failure(harness, event: dict) -> None:
    session_id = event.get("session_id") or ""
    if not session_id:
        harness.emit_empty()
        return
    tool_name = event.get("tool_name")
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    error_text = _extract_error_text(event)
    prompt_id = event.get("prompt_id") or ""

    conn = _connect()
    if conn is None:
        harness.emit_empty()
        return
    try:
        if not _mode_on(conn):
            harness.emit_empty()
            return

        entries = _fetch_entries(conn, strength="notify", timing="tool_fail")
        matched = [
            e for e in entries
            if _matches(e, timing="tool_fail", tool_name=tool_name, tool_input=tool_input, error_text=error_text)
        ]

        if not matched:
            _maybe_bootstrap(conn, harness, session_id)
            return

        candidates = [e for e in matched if not _turn_marked(conn, session_id, prompt_id, e["id"])]
        shown = _select_shown(candidates, budget_chars=TOOL_FAIL_BUDGET_CHARS, render=_notify_render)

        if shown:
            _deliver(conn, session_id, prompt_id, shown)
        conn.commit()

        if shown:
            body = "直前のツール失敗に関連する過去の躓き:\n" + "\n".join(text for _, text in shown)
            harness.emit_additional_context(_wrap(body))
        else:
            harness.emit_empty()
    except sqlite3.Error:
        harness.emit_empty()
    finally:
        conn.close()


def _maybe_bootstrap(conn: sqlite3.Connection, harness, session_id: str) -> None:
    """条件に当たるエントリが1件も無かった場合のみ、セッション1回だけ促しを出す。"""
    row = conn.execute(
        "SELECT 1 FROM feedback_bootstrap_seen WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is not None:
        harness.emit_empty()
        return
    conn.execute("INSERT INTO feedback_bootstrap_seen (session_id) VALUES (?)", (session_id,))
    conn.commit()
    harness.emit_additional_context(_wrap(BOOTSTRAP_MESSAGE))


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------


def _fingerprint(tool_input: dict) -> str:
    canonical = json.dumps(tool_input, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _deny_render(entry: sqlite3.Row) -> str:
    return f"- {entry['body']}"


def _handle_pre_tool_use(harness, event: dict) -> None:
    session_id = event.get("session_id") or ""
    if not session_id:
        harness.emit_empty()
        return
    tool_name = event.get("tool_name")
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    conn = _connect()
    if conn is None:
        harness.emit_empty()
        return
    try:
        if not _mode_on(conn):
            harness.emit_empty()
            return

        entries = _fetch_entries(conn, strength="block", timing="pre_tool")
        hit = [
            e for e in entries
            if _matches(e, timing="pre_tool", tool_name=tool_name, tool_input=tool_input)
        ]
        if not hit:
            harness.emit_empty()
            return

        fingerprint = _fingerprint(tool_input)
        blocking: list[sqlite3.Row] = []
        for entry in hit:
            hold = conn.execute(
                "SELECT fingerprint FROM feedback_holds WHERE session_id = ? AND entry_id = ?",
                (session_id, entry["id"]),
            ).fetchone()
            if hold is not None and hold["fingerprint"] == fingerprint:
                # 直前と同じ引数での再実行 = 押し切り。保留を解除して通す。
                conn.execute(
                    "DELETE FROM feedback_holds WHERE session_id = ? AND entry_id = ?",
                    (session_id, entry["id"]),
                )
                conn.execute(
                    "UPDATE feedback_entries SET overridden_count = overridden_count + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (entry["id"],),
                )
            else:
                # 初回、または前回と異なる引数での再度の当たり = ブロック対象。
                # 保留は新しい指紋で置き換える(過去の指紋は破棄)。
                blocking.append(entry)
                conn.execute(
                    """INSERT INTO feedback_holds (session_id, entry_id, fingerprint)
                       VALUES (?, ?, ?)
                       ON CONFLICT (session_id, entry_id) DO UPDATE SET
                           fingerprint = excluded.fingerprint,
                           created_at = CURRENT_TIMESTAMP""",
                    (session_id, entry["id"], fingerprint),
                )

        if not blocking:
            conn.commit()
            harness.emit_empty()
            return

        shown = _select_shown(blocking, budget_chars=PRE_TOOL_BUDGET_CHARS, render=_deny_render)
        for entry, _text in shown:
            conn.execute(
                "UPDATE feedback_entries SET delivered_count = delivered_count + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (entry["id"],),
            )
        conn.commit()

        reason = "\n".join(text for _, text in shown)
        harness.emit_permission_decision("deny", reason)
    except sqlite3.Error:
        harness.emit_empty()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def main() -> None:
    reader = select_harness()
    try:
        event = reader.read_hook_input()
    except Exception as e:
        print(f"feedback_hook.py error: {e}", file=sys.stderr)
        reader.emit_empty()
        return

    hook_event_name = event.get("hook_event_name") or ""
    harness = select_harness(hook_event_name=hook_event_name)
    try:
        if hook_event_name == "UserPromptSubmit":
            _handle_user_prompt_submit(harness, event)
        elif hook_event_name == "PostToolUseFailure":
            _handle_post_tool_use_failure(harness, event)
        elif hook_event_name == "PreToolUse":
            _handle_pre_tool_use(harness, event)
        else:
            harness.emit_empty()
    except Exception as e:
        print(f"feedback_hook.py error: {e}", file=sys.stderr)
        harness.emit_empty()


if __name__ == "__main__":
    main()
