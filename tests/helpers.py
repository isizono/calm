"""テスト用互換ヘルパー

add_logs / add_decisions のバッチAPIを単件呼び出し形式でラップする。
旧 add_log / add_decision と同じインターフェースを提供する。

検索 retriever / orchestrator のテストで使う SearchContext のファクトリも
ここに集約する。
"""
import asyncio
import functools
from typing import Optional
from src.services.discussion_log_service import add_logs
from src.services.decision_service import add_decisions
from src.services.retract_service import retract
from src.services.search_service import SearchContext


@functools.lru_cache(maxsize=1)
def all_tool_descriptions() -> dict[str, str]:
    """全 MCP ツールの name→description を一括取得しキャッシュする（list_tools を 1 回に抑える）。

    ToolSearch/エージェントから見える tool description 文面を検証するテストで共有する。
    """
    from src.main import mcp

    async def _fetch():
        return {t.name: t.description for t in await mcp.list_tools()}

    return asyncio.run(_fetch())


@functools.lru_cache(maxsize=1)
def all_tool_schemas() -> dict[str, dict]:
    """全 MCP ツールの name→input schema を一括取得しキャッシュする（list_tools を 1 回に抑える）。"""
    from src.main import mcp

    async def _fetch():
        return {t.name: t.parameters for t in await mcp.list_tools()}

    return asyncio.run(_fetch())


def make_search_context(**overrides) -> SearchContext:
    """テスト用のデフォルト SearchContext を生成する。

    overrides で必要なフィールドだけ上書きできる。検索 retriever / orchestrator の
    各テストで重複していた _make_ctx を集約したもの。
    """
    defaults = dict(
        keywords=("alpha",),
        fts_keywords=("alpha",),
        original_keyword_count=None,
        tag_ids=None,
        entity_type=None,
        limit=10,
        offset=0,
        fetch_limit=50,
        keyword_mode="and",
        include_details=False,
        date_after=None,
        date_before=None,
        domain=None,
    )
    defaults.update(overrides)
    return SearchContext(**defaults)


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
