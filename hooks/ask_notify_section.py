"""add_ask通知（notify_wanted）のhook側二重網。

Monitor（notify_pathのtail -F）が起動されなかった・落ちた場合でも、毎ターン
確実に発火するhook（SessionStart/UserPromptSubmit）が拾えるようにするための
共有ロジック。追跡対象ask_id一覧（HookState.tracked_ask_ids）は、そのセッション
自身がadd_ask/unsubscribe_askを呼んだ事実からStop hook（hooks/hook_transcript.py
extract_ask_registrations）が書き足したものであり、本モジュールはそれを
get_asksで直接照会するだけで、identity解決（resolve_identity_by_ancestry等）
には一切触れない。
"""
from __future__ import annotations

import sqlite3

from hooks.hook_state import HookState


def build_ask_notify_lines(session_id: str | None, conn: sqlite3.Connection | None = None) -> list[str]:
    """追跡中askのうちopen以外（answered/dismissed/promoted/withdrawn等、
    何らかの形で解決済み）になっているものの表示行リストを返す。

    表示対象にした（＝get_asksで実際の状態を取りに行った）ask_idは、この
    呼び出し時点でHookStateの追跡対象から外す（消費済みマーク。以降
    SessionStart/UserPromptSubmitのどちらの二重網も同じaskを再表示しない）。
    still-open（未回答）のask_idは追跡対象に残す（次回以降の呼び出しで
    再確認する）。

    conn: 呼び出し元が既に開いているconnを渡すと、それを使い回して
        get_asks_with_connを呼ぶ（自前でget_connection()を呼ばない）。
        省略時（None、既定）はget_asksが自前でconnを開いて閉じる
        （呼び出し元に共有すべきconnが無い場合。例: UserPromptSubmit hook）。

    session_idが空/None、追跡対象が空、get_asksが失敗、該当が0件のいずれかも
    空リストを返す（呼び出し元は「注入すべき内容なし」として扱えばよい）。
    """
    if not session_id:
        return []

    state = HookState(session_id)
    tracked_ids = state.get_tracked_ask_ids()
    if not tracked_ids:
        return []

    from src.services import ask_service

    if conn is not None:
        result = ask_service.get_asks_with_conn(
            conn, ids=tracked_ids, status=None, limit=len(tracked_ids)
        )
    else:
        result = ask_service.get_asks(ids=tracked_ids, status=None, limit=len(tracked_ids))
    if "error" in result:
        return []

    resolved = [a for a in result.get("asks", []) if a.get("status") != "open"]
    if not resolved:
        return []

    lines = [f"askの回答が届いています（{len(resolved)}件）:"]
    lines.extend(f"- {_format_ask_line(ask)}" for ask in resolved)

    resolved_ids = [a["id_raw"] for a in resolved if isinstance(a.get("id_raw"), int)]
    state.remove_tracked_ask_ids(resolved_ids)
    return lines


def _format_ask_line(ask: dict) -> str:
    aid = ask.get("id_raw")
    question = ask.get("question", "")
    status = ask.get("status")
    if status == "answered":
        detail = ask.get("answer_body") or ""
    elif status == "dismissed":
        detail = f"却下: {ask.get('triage_reason') or ''}"
    elif status == "promoted":
        detail = f"promote済み（#{ask.get('promoted_decision_id_raw')}）"
    elif status == "withdrawn":
        detail = f"取り下げ済み: {ask.get('withdraw_reason') or ''}"
    else:
        detail = status or ""
    return f"(#{aid}) {question} → {detail}"
