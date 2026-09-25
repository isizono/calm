"""デルタ通知 middleware

check_inしたactivityを起点に、以降のツール呼び出しのたびにtopicスコープを
引き直し（購読テーブルは持たない）、関連topicへ新規追加されたdecision/log/
materialがあれば、レスポンスにベルとして注入する。watermarkはMCPサーバー
プロセス内のin-memory dictで保持し、DB・migrationは持たない。サーバー再起動で
wipeされ、次のcheck_inで再ベースラインされる（意図した挙動）。
"""
from __future__ import annotations

import contextlib
import sys
import threading
from typing import Any

import mcp.types as mt
from mcp.types import TextContent

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from src.db import get_connection
from src.infra.session_identity import get_caller_session_id
from src.services import delta_service
from src.services.checkin_queries import checkin_scope

# セッション別watermark。キーはget_caller_session_id()の解決結果。Noneが
# 返る呼び出しはwatermarkの読み書き自体を行わない（共有キーに相乗りすると
# 別セッションの既読位置が混ざるため）。
# セッション終了はこのモジュールに通知されないため、tag_service._injected_tagsと
# 同様に上限超過時は挿入順の最古セッションから追い出す
# （放置するとセッション数ぶん永久に成長するため）。
_watermarks: dict[str, dict] = {}
_watermarks_lock = threading.Lock()
_WATERMARKS_MAX_SESSIONS = 256

_CHECK_IN_TOOL_NAMES = frozenset({"check_in"})

# check_inの結果を直接ではなくネストしたキーで返すツール名 → キー名。
# add_activity(check_in=True、デフォルト)はcheck_inの結果をトップレベルではなく
# result["check_in_result"]に入れて返す（activity_service.add_activity参照）ため、
# _CHECK_IN_TOOL_NAMESとは別扱いにしないとbaselineが一切セットされない。
_CHECK_IN_NESTED_KEY_TOOL_NAMES: dict[str, str] = {"add_activity": "check_in_result"}

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

# エンティティ種別 → delta_service.compute_deltaが返すリストのキー名
_DELTA_LIST_KEYS: dict[str, str] = {
    "decision": "new_decisions",
    "log": "new_logs",
    "material": "new_materials",
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

        # デルタ通知は「あったら便利」な後付け機構であり、本来のツール呼び出しは
        # 既に成功している。識別子解決を含め、ここでの例外（DB busy、想定外の
        # レスポンス形状変化等）が全ツール呼び出しを道連れにしないよう、
        # ベストエフォートで握りつぶす。
        try:
            session_key = get_caller_session_id()
            if session_key is None:
                return result
            tool_name = context.message.name

            if tool_name in _CHECK_IN_TOOL_NAMES:
                _handle_check_in(session_key, result)
            elif tool_name in _CHECK_IN_NESTED_KEY_TOOL_NAMES:
                _handle_check_in(session_key, result, nested_key=_CHECK_IN_NESTED_KEY_TOOL_NAMES[tool_name])
            elif tool_name in _WRITE_TOOL_ENTITY_TYPES:
                _handle_write(session_key, tool_name, result)
            else:
                _handle_other(session_key, result)
        except Exception as e:
            print(f"delta_middleware.on_call_tool error: {e}", file=sys.stderr)

        return result


def _handle_check_in(session_key: str, result: Any, nested_key: str | None = None) -> None:
    """check_in結果からactivity_idを読み取り、baselineで初期化する。

    nested_keyが指定された場合、structured_content自体ではなく
    structured_content[nested_key]をcheck_in結果として扱う（add_activity(check_in=True)
    がcheck_in結果をresult["check_in_result"]にネストして返すため）。

    activity_idの読み方自体はcheckin_queries.checkin_scopeに一本化している。check_in
    応答の形が変わってもこのmiddlewareは直接キーを読まないため、応答の形を変えるPRと
    activity_idの読み方を変えるPRが必ず同じになる。checkin_scopeが返すtopic_idsは
    使わない（topicスコープは購読テーブルとして固定せず、以降ツール呼び出しのたびに
    delta_service.derive_scopeで引き直すため）。checkin_scopeがNoneを返す場合
    （error応答・nested_keyの値が辞書でない＝add_activity(check_in=False)相当等）は
    何もしない（直前のwatermarkがあればそのまま残す）。
    """
    structured = getattr(result, "structured_content", None)
    if not structured:
        return
    if nested_key is not None:
        structured = structured.get(nested_key)
        if not isinstance(structured, dict):
            return
    scope = checkin_scope(structured)
    if scope is None:
        return
    activity_id, _ = scope

    # topicスコープはここでは引かない。以降のツール呼び出しのたびに
    # derive_scopeで引き直すため、check_in時点のスコープを保持する意味が無い。
    # get_baselineは純relationalクエリでベクトル検索を使わないため、
    # sqlite-vecネイティブ拡張のロードをスキップしてオープンコストを削減する。
    with contextlib.closing(get_connection(load_vec=False)) as conn:
        baseline = delta_service.get_baseline(conn)

    with _watermarks_lock:
        if session_key not in _watermarks:
            while len(_watermarks) >= _WATERMARKS_MAX_SESSIONS:
                del _watermarks[next(iter(_watermarks))]
        _watermarks[session_key] = {
            "activity_id": activity_id,
            "decision_id": baseline["decision_id"],
            "log_id": baseline["log_id"],
            "material_id": baseline["material_id"],
        }


def _handle_write(session_key: str, tool_name: str, result: Any) -> None:
    """write系ツールの自己通知抑制: 自分の書き込みでscope内に未配信の差分があれば
    先に注入してから、watermarkを実際のDB上のmaxまで前進する。

    自分が作成したidだけでwatermarkを進めると、直前に他セッションがscope内に
    書いた未配信の項目（idが自分の新規idより小さいもの）が、以降のcompute_delta
    の下限を追い越されて黙って読み飛ばされる。先に compute_delta で「今のwatermark
    以降の全件」を取得し、その中から自分が今回作成したid（＝自己通知抑制の対象）
    だけを除いて注入することで、他セッション分を取りこぼさない。
    """
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

    # derive_scope・_scoped_ids・compute_deltaはどれも純relationalクエリのみ（vec不要）。
    # 理由は_handle_check_in参照。topicスコープはここで毎回引き直す（後から
    # boardトピックが関連付けられていれば、その変化がこの呼び出しから反映される）。
    with contextlib.closing(get_connection(load_vec=False)) as conn:
        topic_ids = delta_service.derive_scope(conn, wm["activity_id"])
        scoped_ids = _scoped_ids(conn, entity_type, created_ids, topic_ids, wm["activity_id"])
        if not scoped_ids:
            # scope外への自己書き込みは自己通知抑制の対象にならないため、
            # ここでは何もしない（他セッション分の配達は次のツール呼び出しに委ねる）
            return
        delta = delta_service.compute_delta(conn, topic_ids, wm["activity_id"], wm)

    # 注入する内容だけ、自分が今回作成したid（＝この呼び出し自身の自己通知抑制対象）を
    # 取り除く。他の2種別（例: decisionを書いた呼び出しでのlog/material）はこの
    # 書き込みでは作られていないため、丸ごと他セッション分として扱ってよい。
    # watermarkの前進にはフィルタ前のdeltaを使う（自分のid分もまとめて前進させる。
    # _handle_otherと同じ_advance_watermarkを共有する）。
    list_key = _DELTA_LIST_KEYS[entity_type]
    notify_delta = {**delta, list_key: [item for item in delta[list_key] if item["id"] not in scoped_ids]}

    if notify_delta["new_decisions"] or notify_delta["new_logs"] or notify_delta["new_materials"]:
        _inject(result, notify_delta)

    with _watermarks_lock:
        current = _watermarks.get(session_key)
        if current is None:
            return
        _advance_watermark(current, delta)


def _handle_other(session_key: str, result: Any) -> None:
    """その他のツール呼び出し: scope内の差分があれば注入し、watermarkを前進する。"""
    with _watermarks_lock:
        wm = _watermarks.get(session_key)
    if wm is None:
        return

    # delta_service.derive_scope・compute_deltaも純relationalクエリのみ（vec不要）。
    # PR #550レビュー指摘: check-in済みセッションの以降の全ツール呼び出しで無条件に
    # 発生するmiddleware専用接続のコストのうち、拡張ロード分だけでも削減する
    # （接続オープン自体・クエリのコストは残る。deltaの有無を事前に知る手段が
    # ないため、これ以上の早期リターンは設計を変えないと難しいと判断し見送った）。
    # topicスコープはここで毎回引き直す（購読テーブルは持たない）。
    with contextlib.closing(get_connection(load_vec=False)) as conn:
        topic_ids = delta_service.derive_scope(conn, wm["activity_id"])
        delta = delta_service.compute_delta(conn, topic_ids, wm["activity_id"], wm)

    if not (delta["new_decisions"] or delta["new_logs"] or delta["new_materials"]):
        return

    _inject(result, delta)

    with _watermarks_lock:
        current = _watermarks.get(session_key)
        if current is None:
            return
        _advance_watermark(current, delta)


def _advance_watermark(current: dict, delta: dict) -> None:
    """deltaに含まれる各種別のmax idまでwatermarkを前進する（後退はしない）。"""
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
