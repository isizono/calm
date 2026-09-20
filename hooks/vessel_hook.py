"""器のhook: 観測台帳への書き込みと、書き込みの結び付け・撤回の宣言。

hooks/hooks.json の器の5エントリ（SessionStart・UserPromptSubmit・PostToolUse・
PostToolUseFailure・Stop）を、標準入力の hook_event_name で分けて処理する1本の
スクリプト。書くのは観測（utterance・speaker・reply・tool・tool_overflow・
tool_fail・boundary）、器の書き込みツールの成功結果からの結び付け(bind)、
get_lessonsが配達した知見の観測(delivered)、人間の1行の撤回宣言
(human_withdraw)。配達・踏み跡・訂正の合図はまだ書かない。

停止スイッチが観測だけの状態(observe)のときは、これらの書き込みを行いつつ
標準出力には何も書かない。動かす状態(on)のときだけ、書き込みの判定結果と
撤回の結果を`hookSpecificOutput.additionalContext`で返す。

DBへの書き込みは hooks/citation_event_log.py の形に揃え、src.db を経由せず
sqlite3 を直接使う（起動コストを抑えるため）。例外はすべてfail-open
（止めない・差し戻さない）とし、失敗は ~/.cc-memory/logs/vessel_hook.jsonl に
1行残す。器のテーブルが無ければ何もしない。停止スイッチが読めないときは
止める側（'off'）と同じにふるまう。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.env_compat import env_get
from src.services.vessel_rules import (
    APPEND_LESSON_TOOL,
    GET_LESSONS_TOOL,
    RECORD_LESSON_TOOL,
    TOOL_CALLS_PER_TURN_MAX,
    TOOL_FAIL_MAX_CHARS,
    TOOL_SUMMARY_MAX_CHARS,
    VESSEL_WRITE_TOOLS,
    bodies_match,
    compute_flag,
    find_quote_ref,
    plain_text,
    transcript_body,
)

DEFAULT_DB_PATH = Path.home() / ".claude" / ".claude-code-memory" / "discussion.db"
DEFAULT_LOG_PATH = Path.home() / ".cc-memory" / "logs" / "vessel_hook.jsonl"

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
    return Path(env_get("CALM_VESSEL_LOG_PATH", str(DEFAULT_LOG_PATH)))


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
        row = conn.execute("SELECT mode FROM vessel_meta WHERE id = 1").fetchone()
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


def _handle_session_start(conn: sqlite3.Connection, data: dict, mode: str) -> None:
    session_id = data.get("session_id")
    if not session_id:
        return
    conn.execute(
        "INSERT INTO obs_events (session_id, agent_id, kind) VALUES (?, ?, 'boundary')",
        (session_id, data.get("agent_id")),
    )


# 地の文の1行が前後の空白を除いてこれと完全一致するときだけ撤回の宣言になる
# （否定文・疑問文・行の途中では行全体が一致しないので効かない）。
_WITHDRAW_LINE_RE = re.compile(r"^知見撤回 ([a-z0-9-]{3,40})$")


def _write_human_withdraw_declarations(
    conn: sqlite3.Connection, session_id: str, prompt: str, utterance_id: int
) -> list[str]:
    """地の文の行単独の`知見撤回 <handle>`を観測行にする（行ごとに1つ）。

    書く時点では発話が人間かを見ない（`lesson_current`が都度判定する）。handle
    が無い・既に撤回済みのときは観測行を書かず、結果の文言だけ返す。
    """
    outcomes: list[str] = []
    for line in plain_text(prompt).split("\n"):
        match = _WITHDRAW_LINE_RE.match(line.strip())
        if not match:
            continue
        handle = match.group(1)
        row = conn.execute(
            "SELECT lesson_id, retracted FROM lesson_current WHERE handle = ?", (handle,)
        ).fetchone()
        if row is None:
            outcomes.append(f"{handle}: handleが無い")
            continue
        lesson_id, retracted = row
        if retracted:
            outcomes.append(f"{handle}: 既に撤回済み")
            continue
        conn.execute(
            "INSERT INTO obs_events (session_id, kind, lesson_id, ref_id) "
            "VALUES (?, 'human_withdraw', ?, ?)",
            (session_id, lesson_id, utterance_id),
        )
        outcomes.append(f"{handle}: 撤回になった")
    return outcomes


def _handle_user_prompt_submit(conn: sqlite3.Connection, data: dict, mode: str) -> str | None:
    session_id = data.get("session_id")
    prompt = data.get("prompt")
    if not session_id or prompt is None:
        return None
    prompt_id = data.get("prompt_id")
    agent_id = data.get("agent_id")

    flag = compute_flag(prompt)
    cur = conn.execute(
        "INSERT INTO obs_events (session_id, prompt_id, agent_id, kind, flag, text) "
        "VALUES (?, ?, ?, 'utterance', ?, ?)",
        (session_id, prompt_id, agent_id, flag, prompt),
    )
    utterance_id = cur.lastrowid

    withdraw_outcomes = _write_human_withdraw_declarations(conn, session_id, prompt, utterance_id)

    if prompt_id:
        rows = _read_transcript_lines(data.get("transcript_path"))
        matched = _select_single_user_match(rows, prompt_id, prompt)
        if matched is not None:
            conn.execute(
                "INSERT OR IGNORE INTO obs_events (session_id, kind, text, ref_id) "
                "VALUES (?, 'speaker', ?, ?)",
                (session_id, _speaker_json(matched, data), utterance_id),
            )

    if mode == "on" and withdraw_outcomes:
        return "; ".join(withdraw_outcomes)[:100]
    return None


def _summarize_tool_input(tool_input: object) -> str:
    try:
        summary = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        summary = str(tool_input)
    return summary[:TOOL_SUMMARY_MAX_CHARS]


def _parse_tool_response(tool_response: object) -> dict | None:
    """MCPツールの結果からJSON辞書を取り出す。形が読めなければNone。

    tool_responseは辞書の`content`（文字列、またはtype='text'のブロック配列）に
    JSON文字列が入る形と、tool_response自体がJSON文字列である形の両方が実機で
    観測されている（`hooks/ask_answer_rewake_hook.py`の前例と同じ揺れ）。
    """
    content = tool_response.get("content") if isinstance(tool_response, dict) else tool_response
    if isinstance(content, list):
        content = next(
            (b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"),
            None,
        )
    if not isinstance(content, str):
        return None
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _has_speaker(conn: sqlite3.Connection, utterance_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM obs_events WHERE kind = 'speaker' AND ref_id = ?", (utterance_id,)
    ).fetchone() is not None


def _turn_missing_speaker(conn: sqlite3.Connection, session_id: str, prompt_id: str | None) -> bool:
    """このターンの発話(サブエージェントでない)に、まだspeaker行の無いものがあるか。

    prompt_idが無ければターンを特定できないので、安全側に「未確定」として扱う。
    """
    if not prompt_id:
        return True
    rows = conn.execute(
        "SELECT id FROM obs_events WHERE kind = 'utterance' AND agent_id IS NULL "
        "AND session_id = ? AND prompt_id = ?",
        (session_id, prompt_id),
    ).fetchall()
    return any(not _has_speaker(conn, row[0]) for row in rows)


_PROTECTED_ENTRY_KINDS = ("body", "conditions", "withdraw")


def _bind_feedback(
    conn: sqlite3.Connection,
    session_id: str,
    prompt_id: str | None,
    lesson_id: int,
    entry_id: int | None,
    ref_id: int | None,
    handle: str,
    entry_kind: str | None,
) -> str:
    """判定結果の1行を組み立てる。出自が確定できないターン末待ちの場合はそう返す。"""
    pending = (ref_id is not None and not _has_speaker(conn, ref_id)) or _turn_missing_speaker(
        conn, session_id, prompt_id
    )
    if pending:
        origin_text = "出自はターン末に決まる"
    else:
        is_human = conn.execute(
            "SELECT 1 FROM lesson_basis WHERE lesson_id = ? AND entry_id IS ? "
            "AND session_id = ? AND void = 0",
            (lesson_id, entry_id, session_id),
        ).fetchone() is not None
        origin_text = "出自=人間" if is_human else "出自=AI"

    text = f"[calm:知見 handle={handle} {origin_text}]"
    if entry_id is not None and entry_kind in _PROTECTED_ENTRY_KINDS:
        is_protected = conn.execute(
            "SELECT 1 FROM lesson_protected WHERE lesson_id = ?", (lesson_id,)
        ).fetchone() is not None
        if is_protected:
            text += f" {entry_kind}は守られた知見のため効かない"
    return text


def _handle_bind(conn: sqlite3.Connection, data: dict, mode: str) -> str | None:
    """器の書き込みツール(record_lesson/append_lesson)の成功結果をbindにする。

    引用があれば同じセッションのサブエージェントでない発話から部分文字列で探し、
    見つかった行をref_idにする。人間の発話かどうかはここでは見ない（ビューが
    判定する）。
    """
    result = _parse_tool_response(data.get("tool_response"))
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    session_id = data.get("session_id")
    lesson_id = result.get("lesson_id")
    handle = result.get("handle")
    if not session_id or lesson_id is None or not handle:
        return None
    entry_id = result.get("entry_id")
    entry_kind = result.get("entry_kind")
    prompt_id = data.get("prompt_id")
    agent_id = data.get("agent_id")
    quote = (data.get("tool_input") or {}).get("quote")

    ref_id = find_quote_ref(conn, session_id, quote, prompt_id) if quote else None

    conn.execute(
        "INSERT INTO obs_events (session_id, prompt_id, agent_id, kind, lesson_id, entry_id, ref_id) "
        "VALUES (?, ?, ?, 'bind', ?, ?, ?)",
        (session_id, prompt_id, agent_id, lesson_id, entry_id, ref_id),
    )

    if mode != "on":
        return None
    return _bind_feedback(conn, session_id, prompt_id, lesson_id, entry_id, ref_id, handle, entry_kind)


def _handle_pull(conn: sqlite3.Connection, data: dict) -> None:
    """get_lessonsの成功結果のうち、配達する知見（delivered_handles）を`delivered`にする。"""
    result = _parse_tool_response(data.get("tool_response"))
    if not isinstance(result, dict) or not result.get("ok"):
        return
    session_id = data.get("session_id")
    if not session_id:
        return
    prompt_id = data.get("prompt_id")
    agent_id = data.get("agent_id")
    for item in result.get("delivered_handles") or []:
        handle = item.get("handle") if isinstance(item, dict) else None
        if not handle:
            continue
        row = conn.execute("SELECT id FROM lessons WHERE handle = ?", (handle,)).fetchone()
        if row is None:
            continue
        conn.execute(
            "INSERT INTO obs_events (session_id, prompt_id, agent_id, kind, lesson_id, channel) "
            "VALUES (?, ?, ?, 'delivered', ?, 'pull')",
            (session_id, prompt_id, agent_id, row[0]),
        )


# 器の3ツールの完全な名前の末尾（短い名前）。名前が完全一致しなかった呼び出しを
# 見分けるためだけに使う（プラグイン名・サーバー名が変わって完全一致が崩れても、
# 末尾は残ることが多いため）。
_VESSEL_TOOL_SHORT_NAMES = frozenset(
    name.rsplit("__", 1)[-1] for name in (RECORD_LESSON_TOOL, APPEND_LESSON_TOOL, GET_LESSONS_TOOL)
)


def _looks_like_vessel_tool(tool_name: str) -> bool:
    return tool_name.startswith("mcp__") and tool_name.rsplit("__", 1)[-1] in _VESSEL_TOOL_SHORT_NAMES


def _handle_post_tool_use(conn: sqlite3.Connection, data: dict, mode: str) -> str | None:
    session_id = data.get("session_id")
    tool_name = data.get("tool_name")
    if not session_id or not tool_name:
        return None
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
    else:
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

    if tool_name in VESSEL_WRITE_TOOLS:
        return _handle_bind(conn, data, mode)
    if tool_name == GET_LESSONS_TOOL:
        _handle_pull(conn, data)
        return None
    if _looks_like_vessel_tool(tool_name):
        _log({"at": "bind", "why": "tool_name_mismatch", "tool_name": tool_name})
    return None


def _handle_post_tool_use_failure(conn: sqlite3.Connection, data: dict, mode: str) -> None:
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


def _handle_stop(conn: sqlite3.Connection, data: dict, mode: str) -> None:
    session_id = data.get("session_id")
    if not session_id:
        return
    transcript_path = data.get("transcript_path")

    cursor_row = conn.execute(
        "SELECT byte_offset FROM vessel_cursor WHERE session_id = ?", (session_id,)
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
        "INSERT INTO vessel_cursor (session_id, byte_offset) VALUES (?, ?) "
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
    context = None
    try:
        try:
            context = handler(conn, data, mode)
            conn.commit()
        except Exception as e:
            conn.rollback()
            context = None
            _log({"at": "handle", "event": event, "err": repr(e)})
    finally:
        conn.close()

    # 停止スイッチが動かす状態(on)のときだけ結果を返す。観測だけの状態
    # (observe)では、書き込みは行いつつ標準出力には何も書かない。
    if mode == "on" and context:
        print(json.dumps(
            {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}},
            ensure_ascii=False,
        ))


if __name__ == "__main__":
    main()
