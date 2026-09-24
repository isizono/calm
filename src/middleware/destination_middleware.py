"""宛先候補 middleware

判定待ち(goalブロックのlabelがjudge_ready)の応答にだけ反応し、同じgoalに
紐づくアクティビティへ最後にcheck-inした他セッションを宛先候補として
レスポンスに同梱する。対象はgoal/goal_hintブロックを実際に返す書き込み系
ツールに限る（読み取りツールの応答には同梱しない）。ホワイトリスト対象外の
ツール・goalが返らない・judge_ready以外の大多数の呼び出しではtool_name比較と
dictのキー参照だけで早期returnし、DBクエリ・生存確認・ファイル読み取りを
一切走らせない。

add_logsでboardタグ付きトピックへ投稿したときも同様に宛先候補を返す
（周知をいま生きているセッションへ届ける経路として、SendMessageで直接
話しかけられるようにするため）。この経路はconfig.PEER_NUDGE_ENABLED
（既定OFF）がFalseの間は一切発火しない。config.PEER_NUDGE_ENABLEDがFalseの
とき、goal系の応答（宛先候補・推奨文言）もPR適用前と完全に同一に保つ。
config.PEER_NUDGE_ENABLEDがTrueのときは、goal系の推奨文言にもpeer-nudge
スキルへの誘導を1行追加する。
"""
from __future__ import annotations

import contextlib
import sys
from typing import Any, Optional

import mcp.types as mt
from mcp.types import TextContent

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from src import config
from src.db import get_connection
from src.infra.cli_session import read_cli_session
from src.infra.session_identity import get_caller_session_id
from src.infra.session_manager import DEFAULT_LIVENESS_TIMEOUT_SEC
from src.services.session_registry_service import is_session_alive

# goal/goal_hintブロックを実際に返す書き込み系ツールのみを対象にする。
# 読み取りツール(get_goal等)がjudge_readyなgoalブロックを返しても対象外。
_TARGET_TOOL_NAMES = frozenset({"check_in", "set_goal", "update_goal", "judge_goal", "update_activity"})

# board拡張の対象ツール。config.PEER_NUDGE_ENABLEDがOFFのときは一切参照しない。
_BOARD_TARGET_TOOL_NAME = "add_logs"

# last_heartbeat_atによる事前絞り込みはあくまで安価なフィルタで、最終的な
# 生存確認は各候補ごとにis_session_alive()で行う。しきい値はsession_manager
# のliveness TTL既定値と揃える。
_CANDIDATE_QUERY = """
    SELECT s.session_id, s.cli_session_id, s.cli_pid,
           s.last_checkin_activity_id AS activity_id, a.title AS activity_title
    FROM sessions s
    JOIN goal_activities ga ON ga.activity_id = s.last_checkin_activity_id
    JOIN activities a ON a.id = s.last_checkin_activity_id
    WHERE ga.goal_id = ?
      AND s.ended_at IS NULL
      AND s.cli_session_id IS NOT NULL
      AND s.session_id != ?
      AND s.last_heartbeat_at IS NOT NULL
      AND s.last_heartbeat_at > datetime('now', '-' || ? || ' seconds')
"""

# boardタグ(素タグ、名前空間なし)付きのトピックに belongs_to で紐づくアクティビティへ
# 最後にcheck-inした、生存中の他セッションを候補にする。JOIN先がgoal_activitiesでは
# なくrelationsになる以外は_CANDIDATE_QUERYと同じ絞り込み。
_BOARD_CANDIDATE_QUERY = """
    SELECT s.session_id, s.cli_session_id, s.cli_pid,
           s.last_checkin_activity_id AS activity_id, a.title AS activity_title
    FROM sessions s
    JOIN relations r ON r.source_type = 'activity' AND r.source_id = s.last_checkin_activity_id
                     AND r.target_type = 'topic' AND r.target_id = ?
                     AND r.relation_type = 'belongs_to'
    JOIN activities a ON a.id = s.last_checkin_activity_id
    WHERE s.ended_at IS NULL
      AND s.cli_session_id IS NOT NULL
      AND s.session_id != ?
      AND s.last_heartbeat_at IS NOT NULL
      AND s.last_heartbeat_at > datetime('now', '-' || ? || ' seconds')
"""

_BOARD_TAG_QUERY = """
    SELECT 1 FROM topic_tags tt
    JOIN tags t ON t.id = tt.tag_id
    WHERE tt.topic_id = ? AND t.namespace = '' AND t.name = 'board'
"""

# boardトピックとトピック同士でrelatedな(1段の)他トピックのID。board skillの
# 手順ではboardトピックは元トピックにrelatedで結ばれ、[議論]アクティビティを
# 立てない質問・周知・事前の声かけではboardトピック自身へcheck-inする者がいない
# ため、宛先候補は元トピック側で作業しているセッションから拾う必要がある。
# relations_viewは双方向に展開済みなので正規化方向(activity<topicの並びとは
# 別のtopic-topic間のsource_id<target_id)を気にせず引ける。
_RELATED_TOPIC_QUERY = """
    SELECT target_id FROM relations_view
    WHERE source_type = 'topic' AND source_id = ?
      AND target_type = 'topic' AND relation_type = 'related'
"""


class DestinationCandidateMiddleware(Middleware):
    """判定待ちgoal、およびboardトピックへの投稿に紐づく他セッションを宛先候補として応答に注入する。"""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        result = await call_next(context)

        # 宛先候補は「あったら便利」な後付け情報であり、本来のツール呼び出しは
        # 既に成功している。ここでの例外が全ツール呼び出しを道連れにしない
        # よう、ベストエフォートで握りつぶす（delta_middlewareと同じ方針）。
        try:
            tool_name = context.message.name
            if tool_name in _TARGET_TOOL_NAMES:
                _maybe_inject(result)
            elif tool_name == _BOARD_TARGET_TOOL_NAME and config.PEER_NUDGE_ENABLED:
                _maybe_inject_board(result)
        except Exception as e:
            print(f"destination_middleware.on_call_tool error: {e}", file=sys.stderr)

        return result


def _find_judge_ready_goal_id(result: Any) -> Optional[int]:
    """応答のgoal/goal_hintブロックがjudge_readyならそのgoal_idを返す。"""
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        return None
    for key in ("goal", "goal_hint"):
        block = structured.get(key)
        if isinstance(block, dict) and block.get("label") == "judge_ready":
            goal_id = block.get("goal_id_raw")
            if isinstance(goal_id, int):
                return goal_id
    return None


def _rows_to_candidates(rows: list) -> list[dict]:
    """候補クエリの行を、生存確認・名前解決を経て宛先候補のリストに変換する。

    候補クエリは安価な事前絞り込みに過ぎないため、行ごとにis_session_alive()
    （台帳とは別の、hookが実際に読む生存判定）で最終確認し、read_cli_session()
    で人間可読な宛先名を解決できたものだけを残す。sessionsテーブルは起動器
    プロセス単位の行のみを持つため、親セッションに束ねられるサブエージェントは
    ここに独立した候補としては現れない。
    """
    candidates = []
    for row in rows:
        if not is_session_alive(row["cli_session_id"]):
            continue
        cli_pid = row["cli_pid"]
        cli = read_cli_session(cli_pid) if cli_pid is not None else None
        if cli is None:
            continue
        candidates.append(
            {
                "name": cli["name"],
                "activity_id_raw": row["activity_id"],
                "activity_title": row["activity_title"],
            }
        )
    return candidates


def _fetch_candidates(goal_id: int, caller_session_id: str) -> list[dict]:
    """goal_idに紐づくアクティビティへ最後にcheck-inした、生存中の他セッションを返す。

    caller_session_idによる自己除外は、呼び出し元がlauncher経由（起動器ヘッダ
    あり）である前提に依存する。get_caller_session_id()がヘッダ欠落等でephemeral
    なctx.session_idにフォールバックした場合、その値はsessionsテーブルの
    session_id（常に起動器のUUID、id_kind='bridge'）とは値空間が異なり一致しない
    ため、自己除外が機能しない可能性がある。delta_middlewareの自己通知抑制も
    同じ前提に依存しており、本ミドルウェア固有の制約ではない。
    """
    with contextlib.closing(get_connection(load_vec=False)) as conn:
        rows = conn.execute(
            _CANDIDATE_QUERY,
            (goal_id, caller_session_id, int(DEFAULT_LIVENESS_TIMEOUT_SEC)),
        ).fetchall()
    return _rows_to_candidates(rows)


def _fetch_board_candidates(topic_id: int, caller_session_id: str) -> list[dict]:
    """topic_idに belongs_to で紐づくアクティビティへ最後にcheck-inした、生存中の他セッションを返す。

    caller_session_idによる自己除外の制約は_fetch_candidatesと同じ。
    """
    with contextlib.closing(get_connection(load_vec=False)) as conn:
        rows = conn.execute(
            _BOARD_CANDIDATE_QUERY,
            (topic_id, caller_session_id, int(DEFAULT_LIVENESS_TIMEOUT_SEC)),
        ).fetchall()
    return _rows_to_candidates(rows)


def _is_board_topic(topic_id: int) -> bool:
    """topic_idが素タグ`board`(名前空間なし)を持つかどうかを返す。"""
    with contextlib.closing(get_connection(load_vec=False)) as conn:
        return conn.execute(_BOARD_TAG_QUERY, (topic_id,)).fetchone() is not None


def _related_topic_ids(topic_id: int) -> list[int]:
    """topic_idとトピック同士でrelatedな(1段の)他トピックのIDを返す。"""
    with contextlib.closing(get_connection(load_vec=False)) as conn:
        rows = conn.execute(_RELATED_TOPIC_QUERY, (topic_id,)).fetchall()
    return [row["target_id"] for row in rows]


def _maybe_inject(result: Any) -> None:
    goal_id = _find_judge_ready_goal_id(result)
    if goal_id is None:
        return

    caller_session_id = get_caller_session_id()
    if caller_session_id is None:
        return

    candidates = _fetch_candidates(goal_id, caller_session_id)
    if not candidates:
        return

    lines = [
        f"📮 [宛先候補] 判定待ちのgoalに関連する他セッションが{len(candidates)}件あります。"
        "必要ならSendMessageで知らせてください。"
    ]
    for c in candidates:
        lines.append(f"  - {c['name']}（{c['activity_title']}）")
    if config.PEER_NUDGE_ENABLED:
        lines.append("話しかける前にpeer-nudgeスキルを確認してください。")
    result.content.append(TextContent(type="text", text="\n".join(lines)))
    result.structured_content["destination_candidates"] = candidates


def _maybe_inject_board(result: Any) -> None:
    """add_logsでboardタグ付きトピックへ投稿された場合、宛先候補を応答に同梱する。

    リクエスト引数ではなくレスポンスのcreated配列からtopic_idを拾う。
    実際に作成できたログのtopic_idだけを対象にするため（部分成功時に
    errors側のitemを誤って対象にしない）。
    """
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        return
    created = structured.get("created")
    if not isinstance(created, list) or not created:
        return

    topic_ids = sorted({
        c["topic_id"] for c in created
        if isinstance(c, dict) and isinstance(c.get("topic_id"), int)
    })
    if not topic_ids:
        return

    caller_session_id = get_caller_session_id()
    if caller_session_id is None:
        return

    board_topic_ids = [tid for tid in topic_ids if _is_board_topic(tid)]
    if not board_topic_ids:
        return

    # 質問・周知・事前の声かけでは[議論]アクティビティを立てないため、boardトピック
    # 自身へcheck-inする者がいないことが多い。元トピック(1段関連)側で作業している
    # セッションも候補に含める。
    target_topic_ids = set(board_topic_ids)
    for tid in board_topic_ids:
        target_topic_ids.update(_related_topic_ids(tid))

    candidates = []
    seen_names = set()
    for topic_id in sorted(target_topic_ids):
        for c in _fetch_board_candidates(topic_id, caller_session_id):
            if c["name"] in seen_names:
                continue
            seen_names.add(c["name"])
            candidates.append(c)
    if not candidates:
        return

    lines = [
        f"📮 [宛先候補] 投稿した掲示板トピックに関連する他セッションが{len(candidates)}件あります。"
        "話しかける前にpeer-nudgeスキルを確認してください。"
    ]
    for c in candidates:
        lines.append(f"  - {c['name']}（{c['activity_title']}）")
    result.content.append(TextContent(type="text", text="\n".join(lines)))
    result.structured_content["destination_candidates"] = candidates
