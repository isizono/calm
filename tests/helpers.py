"""テスト用互換ヘルパー

add_logs / add_decisions のバッチAPIを単件呼び出し形式でラップする。
旧 add_log / add_decision と同じインターフェースを提供する。
"""
from typing import Optional
from src.services.discussion_log_service import add_logs
from src.services.decision_service import add_decisions
from src.services.retract_service import retract


def add_log(
    topic_id: int,
    title: Optional[str] = None,
    content: str = "",
    tags: Optional[list[str]] = None,
) -> dict:
    """単件のログ追加（add_logsのラッパー）。旧add_logと同じ戻り値形式を返す。"""
    item = {"topic_id": topic_id, "content": content}
    if title is not None:
        item["title"] = title
    if tags is not None:
        item["tags"] = tags
    result = add_logs([item])
    # バッチAPIのトップレベルエラー（バリデーションエラー等）
    if "error" in result:
        return result
    # アイテムレベルのエラー
    if result["errors"]:
        err = result["errors"][0]["error"]
        return {"error": err}
    # 成功
    return result["created"][0]


def add_decision(
    decision: str,
    reason: str,
    topic_id: int,
    tags: Optional[list[str]] = None,
) -> dict:
    """単件の決定事項追加（add_decisionsのラッパー）。旧add_decisionと同じ戻り値形式を返す。"""
    item = {"topic_id": topic_id, "decision": decision, "reason": reason}
    if tags is not None:
        item["tags"] = tags
    result = add_decisions([item])
    # バッチAPIのトップレベルエラー
    if "error" in result:
        return result
    # アイテムレベルのエラー
    if result["errors"]:
        err = result["errors"][0]["error"]
        return {"error": err}
    # 成功
    return result["created"][0]


def retract_decision(decision_id: int) -> dict:
    """単件のdecision取り消し（retract_serviceのラッパー）。"""
    return retract("decision", [decision_id])
