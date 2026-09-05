"""テスト用互換ヘルパー

add_logs / add_decisions のバッチAPIを単件呼び出し形式でラップする。
旧 add_log / add_decision と同じインターフェースを提供する。

検索 retriever / orchestrator のテストで使う SearchContext のファクトリも
ここに集約する。
"""
import asyncio
import functools
from typing import Optional
from src.db import get_connection
from src.services.discussion_log_service import add_logs
from src.services.decision_service import add_decisions
from src.services.retract_service import retract
from src.services.search_service import SearchContext
from src.services.tag_service import _TAG_NOTES_RATCHET_CEILING


def force_notes_over_ceiling(tag_id: int, notes: str | int) -> None:
    """tags.notesラチェット天井トリガー(migrations/0066)を一時的に外し、対象タグの
    notesを天井超過の内容へ強制する（テスト専用）。

    migration 0066のトリガーはINSERT/UPDATEの両方で「天井を超え、かつ増加する」
    書き込みを拒否するため、通常経路（update_tag等）ではテストDB上で天井超過状態を
    作れない。本番の天井超過タグは、この天井が導入される前から存在していたデータで
    ある（migration適用前のデータは遡って検査されない）。テストではその状況を、
    トリガーの一時削除で再現する。

    Args:
        tag_id: 対象タグID
        notes: 設定するnotes本文。intを渡した場合は"x"を指定桁数繰り返した文字列
            として扱う（天井超過の長さだけが要る単純なケース向けの簡略記法）
    """
    if isinstance(notes, int):
        notes = "x" * notes
    conn = get_connection()
    try:
        conn.execute("DROP TRIGGER IF EXISTS trg_tags_notes_ratchet_ceiling_upd")
        conn.execute(
            "UPDATE tags SET notes = ? WHERE id = ?", (notes, tag_id)
        )
        conn.commit()
    finally:
        conn.execute(
            f"""
            CREATE TRIGGER trg_tags_notes_ratchet_ceiling_upd
            BEFORE UPDATE OF notes ON tags
            FOR EACH ROW
            WHEN NEW.notes IS NOT NULL
                 AND LENGTH(NEW.notes) > {_TAG_NOTES_RATCHET_CEILING}
                 AND (OLD.notes IS NULL OR LENGTH(NEW.notes) > LENGTH(OLD.notes))
            BEGIN
                SELECT RAISE(ABORT, 'tag notes ratchet ceiling exceeded and increasing');
            END;
            """
        )
        conn.commit()
        conn.close()


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
