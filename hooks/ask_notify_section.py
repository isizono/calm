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


def build_ask_notify_lines(
    session_id: str | None,
    conn: sqlite3.Connection | None = None,
    budget_chars: int | None = None,
) -> list[str]:
    """追跡中askのうちopen以外（answered/dismissed/promoted/withdrawn等、
    何らかの形で解決済み）になっているものの表示行リストを返す。

    返り値に実際に含めたask_idだけを、この呼び出し時点でHookStateの追跡
    対象から外す（消費済みマーク。以降SessionStart/UserPromptSubmitの
    どちらの二重網も同じaskを再表示しない）。still-open（未回答）の
    ask_id、およびbudget_chars指定時に予算超過で返り値へ含められなかった
    ask_idは、引き続き追跡対象に残す（次回以降の呼び出しで再確認・再表示
    する）。

    budget_chars: 呼び出し元が戻り値をそのまま渡す先で課される文字数予算
        （例: SessionStart hookのcompose()。当該セクションの実出力が
        budget_charsを超えるとcompose()側でハード切り詰めされ、末尾が
        欠落しうる）。指定すると、呼び出し元が組み立てる最終テキスト
        （"\\n".join(lines) + "\\n"）がbudget_chars以内に収まる先頭からの
        行だけを返し、それらのask_idだけを消費する。収まらない行は
        （切り詰めで表示が欠落しうるため）返り値に含めず、そのask_id以降は
        追跡対象に残したままにする。省略時（None、既定）は予算を意識せず
        全件を返し全件消費する（呼び出し元がこの戻り値を切り詰めずにそのまま
        注入する場合。例: UserPromptSubmit hookはcompose()を経由しないため
        予算制約が無い）。

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

    header = f"askの回答が届いています（{len(resolved)}件）:"
    item_lines = [f"- {_format_ask_line(ask)}" for ask in resolved]

    if budget_chars is None:
        lines = [header, *item_lines]
        resolved_ids = [a["id_raw"] for a in resolved if isinstance(a.get("id_raw"), int)]
        state.remove_tracked_ask_ids(resolved_ids)
        return lines

    lines = [header]
    consumed = []
    for ask, item_line in zip(resolved, item_lines):
        candidate = lines + [item_line]
        if len("\n".join(candidate) + "\n") > budget_chars:
            break
        lines = candidate
        consumed.append(ask)

    if not consumed:
        return []

    if len(consumed) != len(resolved):
        # 全件は収まらなかった。ヘッダーの件数表記を実際に含めた件数へ
        # 差し替える（len(consumed) <= len(resolved)なので桁数は増えず、
        # 上のループで確定した予算内に収まる状態のまま）。
        lines[0] = f"askの回答が届いています（{len(consumed)}件）:"

    resolved_ids = [a["id_raw"] for a in consumed if isinstance(a.get("id_raw"), int)]
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
