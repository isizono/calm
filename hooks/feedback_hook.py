"""フィードバック機構のhook本体（1ファイルでUserPromptSubmit・PostToolUseFailure・
PreToolUseの3イベントを処理する）。

標準入力JSONの`hook_event_name`で処理を振り分ける。DB接続失敗・feedback_switch未作成・
mode値が不正のいずれも mode='off' 相当としてfail-open（何も出さず終了、blockも
効かない）。他のDB書き込みhook（citation_event_log.py等）と同じ方針で、起動コストを
抑えるため src.db を経由せずsqlite3を直接使う。

例外処理方針: main()全体をtry/exceptで囲み、想定外の例外はすべてharness.emit_empty()
で握って終了する（hook自体の不具合で他のtool実行を止めないため）。保存済みエントリの
条件JSON評価で例外（壊れた正規表現等）が出た場合は、そのエントリだけ評価をスキップし
他のエントリの評価は継続する（_matches内で握る）。

サブエージェント発（agent_type付き）の呼び出しでも、エントリ本文は配達する。
出さないのは、ノート・エントリの書き込みを促す行（手入れの行・bootstrapの促し）
だけである（書き込みを禁止されたサブエージェントには実行できないため）。
UserPromptSubmitでは、agent_typeの有無に関わらず、プロンプトが人間の発話でない
ターン（hooks/turn_origin.py参照）のときは発話タイミングの配達を止める。

outputタイミング（Claude自身の直前の出力文への照合）もUserPromptSubmitで扱う。
前回照合した位置以降にtranscriptへ追記されたassistantのtextブロックだけを読み、
既読位置（byte_offset）だけをDBに持つ。transcriptの本文は保存しない。人間の
発話でないターン（bg・orchへの通知や中継）にも届ける。サブエージェント発
（agent_type付き）では照合しない。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.turn_origin import is_nonhuman_turn  # noqa: E402
from src.env_compat import env_get  # noqa: E402
from src.harness import select_harness  # noqa: E402
from src.services.feedback_rules import (  # noqa: E402
    PENDING_STUMBLES_SQL,
    evaluate_condition,
    maintenance_hint,
)

DEFAULT_DB_PATH = Path.home() / ".claude" / ".claude-code-memory" / "discussion.db"

MAX_SHOWN = 3
UTTERANCE_BUDGET_CHARS = 1000
TOOL_FAIL_BUDGET_CHARS = 600
PRE_TOOL_BUDGET_CHARS = 600
OUTPUT_BUDGET_CHARS = 600
# 同じセッションで同じoutputエントリを再配達するまでに空けるUserPromptSubmitの回数。
# 機構や教訓そのものを話題にするセッションで、自分の言及に毎ターン反応して
# 雑音になるのを抑える。
OUTPUT_COOLDOWN_TURNS = 5

BOOTSTRAP_MESSAGE = (
    "躓いた・エラーに遭遇したら、get_feedback_entriesで既存を確かめ、"
    "あればadd_feedback_noteでノートを足し、無ければwrite_feedback_entryで"
    "知見を書いてください。"
)


def _resolve_db_path() -> str:
    return env_get("CALM_DB_PATH", str(DEFAULT_DB_PATH))


def _connect() -> sqlite3.Connection | None:
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
        row = conn.execute("SELECT mode FROM feedback_switch WHERE id = 1").fetchone()
    except sqlite3.Error:
        return False
    return row is not None and row["mode"] == "on"


def _wrap(body: str) -> str:
    return f"<system-reminder>{body}</system-reminder>"


def _fetch_entries(conn: sqlite3.Connection, *, strength: str, timing: str) -> list[sqlite3.Row]:
    # PENDING_STUMBLES_SQLはエイリアス`e`(=feedback_entries)を前提にした相関サブクエリなので、
    # このクエリの`FROM feedback_entries e`は変更しないこと。
    return conn.execute(
        f"SELECT e.*, {PENDING_STUMBLES_SQL} AS pending_stumbles FROM feedback_entries e "
        "WHERE e.strength = ? AND e.timing = ? AND e.deleted_at IS NULL ORDER BY e.id",
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


def _mark_key(event: dict, session_id: str) -> str:
    """区切り重複の管理キー。サブエージェント発は親と別に数える（同じ区切りで親が受け取れなくなるのを避ける）。"""
    agent_id = event.get("agent_id")
    return f"{session_id}:{agent_id}" if isinstance(agent_id, str) and agent_id else session_id


def _notify_renderer(event: dict):
    """notify配達の1件分を整形する関数を返す。サブエージェント発では手入れの行を付けない。"""
    with_hint = not event.get("agent_type")

    def render(entry: sqlite3.Row) -> str:
        line = f"- {entry['body']}({entry['delivered_count'] + 1}回目)"
        hint = maintenance_hint(entry["delivered_count"] + 1, entry["pending_stumbles"]) if with_hint else ""
        return line + ("\n" + hint if hint else "")

    return render


# ---------------------------------------------------------------------------
# output（transcript差分の照合）
# ---------------------------------------------------------------------------


def _read_new_assistant_texts(path: str, offset: int) -> tuple[list[str], int]:
    """offset以降に追記された、改行まで読めた行のassistantのtextブロックを返す。

    戻り値: (textブロックの本文の一覧, 新しいoffset)。書きかけの末尾行は
    offsetを進めず次回に回す。JSONとして読めない行は読み飛ばす（offsetは進める）。
    thinking・tool_use・tool_result・user行は見ない。ファイルが読めない・offsetより
    小さくなっていた場合は現在の末尾へ位置を取り直し、本文は返さない。
    """
    try:
        size = Path(path).stat().st_size
        if offset > size:
            return [], size
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset

    end = data.rfind(b"\n") + 1
    texts: list[str] = []
    for raw_line in data[:end].split(b"\n"):
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
    return texts, offset + end


def _output_candidates(conn: sqlite3.Connection, session_id: str, transcript_path: str) -> list[sqlite3.Row]:
    """今回のUserPromptSubmitで届けるoutputエントリを返す。

    既読位置と通し番号は、エントリの有無・当たりの有無に関わらず毎回進める
    （後からエントリが作られたときに、過去の発言の山へ一斉に当たらないため）。
    初めて見るセッションは現在の末尾から始める（再開したセッションの過去分を
    掘り返さない）。
    """
    row = conn.execute(
        "SELECT byte_offset, turn_seq FROM feedback_output_cursor WHERE session_id = ?", (session_id,)
    ).fetchone()
    turn_seq = (row["turn_seq"] if row else 0) + 1

    entries = _fetch_entries(conn, strength="notify", timing="output")
    if row is None or not entries:
        # ponytail: 本文を読まず現在のファイル末尾へ進める。書きかけの行の途中に
        # 着地しうるが、次回その行はJSONとして読めず読み飛ばされるだけで済む。
        try:
            new_offset = Path(transcript_path).stat().st_size
        except OSError:
            new_offset = row["byte_offset"] if row else 0
        texts: list[str] = []
    else:
        texts, new_offset = _read_new_assistant_texts(transcript_path, row["byte_offset"])

    conn.execute(
        """INSERT INTO feedback_output_cursor (session_id, byte_offset, turn_seq)
           VALUES (?, ?, ?)
           ON CONFLICT (session_id) DO UPDATE SET
               byte_offset = excluded.byte_offset,
               turn_seq = excluded.turn_seq,
               updated_at = CURRENT_TIMESTAMP""",
        (session_id, new_offset, turn_seq),
    )
    if not texts:
        return []

    candidates = []
    for entry in entries:
        cooldown = conn.execute(
            "SELECT last_turn_seq FROM feedback_output_cooldowns WHERE session_id = ? AND entry_id = ?",
            (session_id, entry["id"]),
        ).fetchone()
        if cooldown is not None and turn_seq - cooldown["last_turn_seq"] < OUTPUT_COOLDOWN_TURNS:
            continue
        if any(_matches(entry, timing="output", output_text=text) for text in texts):
            candidates.append(entry)
    return candidates


def _mark_output_delivered(conn: sqlite3.Connection, session_id: str, shown: list[tuple[sqlite3.Row, str]]) -> None:
    for entry, _text in shown:
        conn.execute(
            "UPDATE feedback_entries SET delivered_count = delivered_count + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (entry["id"],),
        )
        conn.execute(
            """INSERT INTO feedback_output_cooldowns (session_id, entry_id, last_turn_seq)
               VALUES (?, ?, (SELECT turn_seq FROM feedback_output_cursor WHERE session_id = ?))
               ON CONFLICT (session_id, entry_id) DO UPDATE SET last_turn_seq = excluded.last_turn_seq""",
            (session_id, entry["id"], session_id),
        )


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
    nonhuman = is_nonhuman_turn(prompt)
    transcript_path = event.get("transcript_path")
    if not isinstance(transcript_path, str):
        transcript_path = ""
    prompt_id = event.get("prompt_id") or ""

    conn = _connect()
    if conn is None:
        harness.emit_empty()
        return
    try:
        if not _mode_on(conn):
            harness.emit_empty()
            return

        shown: list[tuple[sqlite3.Row, str]] = []
        if not nonhuman:
            entries = _fetch_entries(conn, strength="notify", timing="utterance")
            candidates = [
                e for e in entries
                if not _turn_marked(conn, _mark_key(event, session_id), prompt_id, e["id"])
                and _matches(e, timing="utterance", prompt_text=prompt)
            ]
            shown = _select_shown(candidates, budget_chars=UTTERANCE_BUDGET_CHARS, render=_notify_renderer(event))

        shown_output: list[tuple[sqlite3.Row, str]] = []
        if transcript_path and not event.get("agent_type"):
            output_candidates = _output_candidates(conn, session_id, transcript_path)
            shown_output = _select_shown(output_candidates, budget_chars=OUTPUT_BUDGET_CHARS, render=_notify_renderer(event))

        if shown:
            _deliver(conn, _mark_key(event, session_id), prompt_id, shown)
        if shown_output:
            _mark_output_delivered(conn, session_id, shown_output)
        conn.commit()
        if not shown and not shown_output:
            harness.emit_empty()
            return

        sections = []
        if shown:
            sections.append("過去の躓きから学んだ注意点:\n" + "\n".join(text for _, text in shown))
        if shown_output:
            sections.append("直前の自分の発言に関連する過去の躓き:\n" + "\n".join(text for _, text in shown_output))
        harness.emit_additional_context(_wrap("\n".join(sections)))
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
            if event.get("agent_type"):
                harness.emit_empty()
            else:
                _maybe_bootstrap(conn, harness, session_id)
            return

        candidates = [e for e in matched if not _turn_marked(conn, _mark_key(event, session_id), prompt_id, e["id"])]
        shown = _select_shown(candidates, budget_chars=TOOL_FAIL_BUDGET_CHARS, render=_notify_renderer(event))

        if shown:
            _deliver(conn, _mark_key(event, session_id), prompt_id, shown)
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


def _deny_renderer(event: dict):
    """block配達の1件分を整形する関数を返す。サブエージェント発では手入れの行を付けない。"""
    with_hint = not event.get("agent_type")

    def render(entry: sqlite3.Row) -> str:
        line = f"- {entry['body']}"
        hint = maintenance_hint(entry["delivered_count"] + 1, entry["pending_stumbles"]) if with_hint else ""
        return line + ("\n" + hint if hint else "")

    return render


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

        shown = _select_shown(blocking, budget_chars=PRE_TOOL_BUDGET_CHARS, render=_deny_renderer(event))
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
