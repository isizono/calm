"""デルタ通知 middleware

check_inでスナップショットしたtopicスコープに対し、以降のツール呼び出しで
関連topicへ新規追加されたdecision/log/materialがあれば、レスポンスに
ベルとして注入する。watermarkはMCPサーバープロセス内のin-memory dictで
保持し、DB・migrationは持たない。サーバー再起動でwipeされ、次のcheck_inで
再ベースラインされる（意図した挙動）。
"""
from __future__ import annotations

import sys
import threading
from typing import Any

import mcp.types as mt
from mcp.types import TextContent

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from src.db import get_connection
from src.services import delta_service

# セッション別watermark（ctx.session_idキー）。tag_service._injected_tagsと同じ
# in-memory dict + ロックのパターン。セッション終了はこのモジュールに通知されない
# ため、_injected_tagsと同様に上限超過時は挿入順の最古セッションから追い出す
# （放置するとセッション数ぶん永久に成長するため）。
_watermarks: dict[str, dict] = {}
_watermarks_lock = threading.Lock()
_WATERMARKS_MAX_SESSIONS = 256

_CHECK_IN_TOOL_NAMES = frozenset({"check_in"})

# write系ツール名 → 対応するエンティティ種別
_WRITE_TOOL_ENTITY_TYPES: dict[str, str] = {
    "add_decisions": "decision",
    "add_logs": "log",
    "add_material": "material",
}

# エンティティ種別 → created配列内のidキー名（watermark辞書のキー名とも一致する）
_ID_KEYS: dict[str, str] = {
    "decision": "decision_id",
    "log": "log_id",
    "material": "material_id",
}


class DeltaNotificationMiddleware(Middleware):
    """check-in以降の関連topicスコープの鮮度差分をツールレスポンスに注入する。

    注意: `_handle_check_in`/`_handle_write`/`_handle_other`は内部に`await`を
    含めないこと。asyncioは単一スレッドで動くため、await地点が無ければ
    「読み取り→DB→書き戻し」が他のコルーチンに実行を譲らず事実上atomicになり、
    同一session_idへの並行呼び出しでもannounce-once保証が崩れない。
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        result = await call_next(context)

        session_key = _session_key(context)
        tool_name = context.message.name

        # デルタ通知は「あったら便利」な後付け機構であり、本来のツール呼び出しは
        # 既に成功している。ここでの例外（DB busy、想定外のレスポンス形状変化等）が
        # 全ツール呼び出しを道連れにしないよう、ベストエフォートで握りつぶす。
        try:
            if tool_name in _CHECK_IN_TOOL_NAMES:
                _handle_check_in(session_key, result)
            elif tool_name in _WRITE_TOOL_ENTITY_TYPES:
                _handle_write(session_key, tool_name, result)
            else:
                _handle_other(session_key, result)
        except Exception as e:
            print(f"delta_middleware.on_call_tool error: {e}", file=sys.stderr)

        return result


def _session_key(context: MiddlewareContext) -> str:
    try:
        session_id = context.fastmcp_context.session_id if context.fastmcp_context else None
    except Exception:
        session_id = None
    return session_id or "__default__"


def _handle_check_in(session_key: str, result: Any) -> None:
    """check_in結果からscope（topic_ids/activity_id）を読み取り、baselineで初期化する。

    activityのid_rawが取れない場合（error応答等）は何もしない
    （直前のwatermarkがあればそのまま残す）。
    """
    structured = getattr(result, "structured_content", None)
    if not structured:
        return
    activity = structured.get("activity")
    if not isinstance(activity, dict):
        return
    activity_id = activity.get("id_raw")
    if activity_id is None:
        return

    topic_ids = [
        t["id_raw"] for t in structured.get("related_topics", []) or []
        if isinstance(t, dict) and "id_raw" in t
    ]

    conn = get_connection()
    try:
        baseline = delta_service.get_baseline(conn, topic_ids, activity_id)
    finally:
        conn.close()

    with _watermarks_lock:
        if session_key not in _watermarks:
            while len(_watermarks) >= _WATERMARKS_MAX_SESSIONS:
                del _watermarks[next(iter(_watermarks))]
        _watermarks[session_key] = {
            "topic_ids": topic_ids,
            "activity_id": activity_id,
            "decision_id": baseline["decision_id"],
            "log_id": baseline["log_id"],
            "material_id": baseline["material_id"],
        }


def _handle_write(session_key: str, tool_name: str, result: Any) -> None:
    """write系ツールの自己通知抑制: 自分が作成したid（scope内のみ）でwatermarkを前進する。"""
    with _watermarks_lock:
        wm = _watermarks.get(session_key)
    if wm is None:
        return

    entity_type = _WRITE_TOOL_ENTITY_TYPES[tool_name]
    id_key = _ID_KEYS[entity_type]

    structured = getattr(result, "structured_content", None)
    if not structured:
        return

    if entity_type == "material":
        # add_materialはcreated配列を持たず、トップレベルに material_id を直接返す
        raw_id = structured.get(id_key)
        created_ids = [raw_id] if isinstance(raw_id, int) else []
    else:
        created_ids = [
            item[id_key] for item in structured.get("created", []) or []
            if isinstance(item, dict) and id_key in item
        ]
    if not created_ids:
        return

    conn = get_connection()
    try:
        scoped_ids = _scoped_ids(conn, entity_type, created_ids, wm["topic_ids"], wm["activity_id"])
    finally:
        conn.close()
    if not scoped_ids:
        return

    with _watermarks_lock:
        current = _watermarks.get(session_key)
        if current is None:
            return
        current[id_key] = max(current.get(id_key, 0), max(scoped_ids))


def _handle_other(session_key: str, result: Any) -> None:
    """その他のツール呼び出し: scope内の差分があれば注入し、watermarkを前進する。"""
    with _watermarks_lock:
        wm = _watermarks.get(session_key)
    if wm is None:
        return

    conn = get_connection()
    try:
        delta = delta_service.compute_delta(conn, wm["topic_ids"], wm["activity_id"], wm)
    finally:
        conn.close()

    if not (delta["new_decisions"] or delta["new_logs"] or delta["new_materials"]):
        return

    _inject(result, delta)

    with _watermarks_lock:
        current = _watermarks.get(session_key)
        if current is None:
            return
        if delta["new_decisions"]:
            current["decision_id"] = max(
                current["decision_id"], max(d["id"] for d in delta["new_decisions"])
            )
        if delta["new_logs"]:
            current["log_id"] = max(
                current["log_id"], max(l["id"] for l in delta["new_logs"])
            )
        if delta["new_materials"]:
            current["material_id"] = max(
                current["material_id"], max(m["id"] for m in delta["new_materials"])
            )


def _scoped_ids(
    conn, entity_type: str, ids: list[int], topic_ids: list[int], activity_id: int | None
) -> set[int]:
    """created idのうち、指定scope（topic_ids/activity_id）に属するものだけを返す。

    add_decisionsの応答はレスポンス軽量化でtopic_idを含まないため、ここでは
    リクエスト引数を当てにせず、作成直後のidでrelations/relations_viewへ
    再問い合わせして判定する。
    """
    if not ids:
        return set()
    id_placeholders = ",".join("?" * len(ids))

    if entity_type in ("decision", "log"):
        if not topic_ids:
            return set()
        topic_placeholders = ",".join("?" * len(topic_ids))
        rows = conn.execute(
            f"""
            SELECT DISTINCT source_id FROM relations
            WHERE source_type = ? AND source_id IN ({id_placeholders})
              AND target_type = 'topic' AND relation_type = 'belongs_to'
              AND target_id IN ({topic_placeholders})
            """,
            (entity_type, *ids, *topic_ids),
        ).fetchall()
        return {row["source_id"] for row in rows}

    if entity_type == "material":
        scope_sql, scope_params = delta_service.material_scope_clause(topic_ids, activity_id)
        if not scope_sql:
            return set()
        rows = conn.execute(
            f"""
            SELECT DISTINCT rv.target_id AS id
            FROM relations_view rv
            WHERE ({scope_sql}) AND rv.target_type = 'material'
              AND rv.target_id IN ({id_placeholders})
            """,
            (*scope_params, *ids),
        ).fetchall()
        return {row["id"] for row in rows}

    return set()


def _inject(result: Any, delta: dict) -> None:
    lines = ["📨 [デルタ通知] check-in以降、関連トピックに新しい記録が追加されました。"]
    for d in delta["new_decisions"]:
        lines.append(f"  - decision: {d['title']}（get_decisionsで取得可）")
    for l in delta["new_logs"]:
        lines.append(f"  - log: {l['title']}")
    for m in delta["new_materials"]:
        lines.append(f"  - material: {m['title']}（get_materialで取得可）")
    lines.append("ユーザーへの応答を返す前に、内容を確認してください。")
    result.content.append(TextContent(type="text", text="\n".join(lines)))
    if result.structured_content is not None:
        result.structured_content["delta"] = delta
