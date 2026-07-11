"""main.py の archived_tags 集約・archived フィールド付与のテスト

- get_topics/get_logs/get_decisions/get_activities/get_by_ids の応答トップレベル
  archived_tags 集約（エッジケース#8）
- pull_precedents のdecision item単位のarchived_tags（エッジケース#9）
- search_tags/analyze_tags(orphans) のタグ単位 archived/archived_reason
- update_tag MCPツール定義の archived/archived_reason 引数の受け渡し
"""
import pytest

from src.services.tag_service import update_tag as _svc_update_tag, _injected_tags
from src.services.topic_service import add_topic
from src.services.discussion_log_service import add_logs
from src.services.decision_service import add_decisions
from src.services.activity_service import add_activity
from tests.helpers import add_decision


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化"""
    import src.services.embedding_service as emb
    monkeypatch.setattr(emb, "_server_initialized", False)
    monkeypatch.setattr(emb, "_backfill_done", True)
    monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)


@pytest.fixture(autouse=True)
def _clear_injected_tags():
    _injected_tags.clear()
    yield


def _archive(tag: str, reason: str = "退役済み") -> None:
    result = _svc_update_tag(tag, archived=True, archived_reason=reason)
    assert "error" not in result, result


# ========================================
# get系5関数の archived_tags トップレベル集約
# ========================================


class TestGetTopicsArchivedTags:
    def test_no_archived_tags_returns_empty_list(self, temp_db):
        from src.main import get_topics

        add_topic(title="ActiveTopicForArchivedCheck", description="Desc", tags=["domain:main-active"])

        result = get_topics()
        assert "error" not in result
        assert result["archived_tags"] == []

    def test_archived_tag_present_in_summary(self, temp_db):
        from src.main import get_topics

        add_topic(title="LegacyTopicForArchivedCheck", description="Desc", tags=["domain:main-legacy"])
        _archive("domain:main-legacy")

        result = get_topics()
        assert "error" not in result
        tags_seen = {a["tag"] for a in result["archived_tags"]}
        assert "domain:main-legacy" in tags_seen
        entry = next(a for a in result["archived_tags"] if a["tag"] == "domain:main-legacy")
        assert entry["archived_reason"] == "退役済み"


class TestGetLogsArchivedTags:
    def test_archived_tag_present_in_summary(self, temp_db):
        from src.main import get_logs

        topic = add_topic(title="LogsArchivedTopic", description="Desc", tags=["domain:main-legacy-log"])
        topic_id = topic["topic_id"]
        add_result = add_logs([
            {"topic_id": topic_id, "content": "content", "tags": ["domain:main-legacy-log"]}
        ])
        assert "error" not in add_result
        _archive("domain:main-legacy-log")

        result = get_logs("topic", topic_id)
        assert "error" not in result
        tags_seen = {a["tag"] for a in result["archived_tags"]}
        assert "domain:main-legacy-log" in tags_seen

    def test_no_archived_tags_returns_empty_list(self, temp_db):
        from src.main import get_logs

        topic = add_topic(title="LogsActiveTopic", description="Desc", tags=["domain:main-active-log"])
        topic_id = topic["topic_id"]
        add_logs([{"topic_id": topic_id, "content": "content", "tags": ["domain:main-active-log"]}])

        result = get_logs("topic", topic_id)
        assert "error" not in result
        assert result["archived_tags"] == []


class TestGetDecisionsArchivedTags:
    def test_archived_tag_present_in_summary(self, temp_db):
        from src.main import get_decisions

        topic = add_topic(title="DecisionsArchivedTopic", description="Desc", tags=["domain:main-legacy-dec"])
        topic_id = topic["topic_id"]
        add_result = add_decisions([
            {"topic_id": topic_id, "decision": "d", "reason": "r", "tags": ["domain:main-legacy-dec"]}
        ])
        assert "error" not in add_result
        _archive("domain:main-legacy-dec")

        result = get_decisions("topic", topic_id)
        assert "error" not in result
        tags_seen = {a["tag"] for a in result["archived_tags"]}
        assert "domain:main-legacy-dec" in tags_seen


class TestGetActivitiesArchivedTags:
    def test_archived_tag_present_in_summary(self, temp_db):
        from src.main import get_activities

        add_activity(
            title="ActivityArchivedCheck", description="Desc",
            tags=["domain:main-legacy-act"], check_in=False,
        )
        _archive("domain:main-legacy-act")

        result = get_activities(tags=["domain:main-legacy-act"])
        assert "error" not in result
        tags_seen = {a["tag"] for a in result["archived_tags"]}
        assert "domain:main-legacy-act" in tags_seen

    def test_no_archived_tags_returns_empty_list(self, temp_db):
        from src.main import get_activities

        add_activity(
            title="ActivityActiveCheck", description="Desc",
            tags=["domain:main-active-act"], check_in=False,
        )

        result = get_activities(tags=["domain:main-active-act"])
        assert "error" not in result
        assert result["archived_tags"] == []


class TestGetByIdsArchivedTags:
    def test_archived_tag_present_in_summary(self, temp_db):
        from src.main import get_by_ids

        topic = add_topic(title="ByIdsArchivedTopic", description="Desc", tags=["domain:main-legacy-byid"])
        topic_id = topic["topic_id"]
        _archive("domain:main-legacy-byid")

        result = get_by_ids([{"type": "topic", "id": topic_id}])
        assert "error" not in result
        tags_seen = {a["tag"] for a in result["archived_tags"]}
        assert "domain:main-legacy-byid" in tags_seen

    def test_no_archived_tags_returns_empty_list(self, temp_db):
        from src.main import get_by_ids

        topic = add_topic(title="ByIdsActiveTopic", description="Desc", tags=["domain:main-active-byid"])
        topic_id = topic["topic_id"]

        result = get_by_ids([{"type": "topic", "id": topic_id}])
        assert "error" not in result
        assert result["archived_tags"] == []


# ========================================
# pull_precedents のdecision item単位 archived_tags
# ========================================


class TestPullPrecedentsArchivedTags:
    def test_full_decision_gets_item_level_archived_tags(self, temp_db, monkeypatch):
        from src.main import pull_precedents
        from src.services import precedent_pull_service as pps

        topic = add_topic(title="PrecedentArchivedTopic", description="desc", tags=["domain:main-legacy-prec"])
        topic_id = topic["topic_id"]
        add_decision(
            decision="precedent decision", reason="reason",
            topic_id=topic_id, tags=["domain:main-legacy-prec"],
        )
        _archive("domain:main-legacy-prec")
        monkeypatch.setattr(pps, "encode_query", lambda context: None)

        result = pull_precedents("何らかの論点についての文脈", topic_ids=[topic_id])
        assert "error" not in result
        decisions = result["topics"][0]["decisions"]
        assert len(decisions) == 1
        dec = decisions[0]
        assert dec["detail"] == "full"
        tags_seen = {a["tag"] for a in dec["archived_tags"]}
        assert "domain:main-legacy-prec" in tags_seen

    def test_full_decision_without_archived_tags_gets_empty_list(self, temp_db, monkeypatch):
        from src.main import pull_precedents
        from src.services import precedent_pull_service as pps

        topic = add_topic(title="PrecedentActiveTopic", description="desc", tags=["domain:main-active-prec"])
        topic_id = topic["topic_id"]
        add_decision(
            decision="active decision", reason="reason",
            topic_id=topic_id, tags=["domain:main-active-prec"],
        )
        monkeypatch.setattr(pps, "encode_query", lambda context: None)

        result = pull_precedents("何らかの論点についての文脈", topic_ids=[topic_id])
        assert "error" not in result
        dec = result["topics"][0]["decisions"][0]
        assert dec["archived_tags"] == []


# ========================================
# search_tags / analyze_tags のタグ単位 archived フィールド
# ========================================


class TestSearchTagsArchivedField:
    def test_archived_tag_flagged(self, temp_db):
        from src.main import search_tags

        add_topic(title="SearchTagsArchivedTopic", description="Desc", tags=["domain:main-searchtag-legacy"])
        _archive("domain:main-searchtag-legacy")

        result = search_tags("main-searchtag-legacy")
        assert "error" not in result
        entry = next(t for t in result["tags"] if t["tag"] == "domain:main-searchtag-legacy")
        assert entry["archived"] is True
        assert entry["archived_reason"] == "退役済み"

    def test_non_archived_tag_not_flagged(self, temp_db):
        from src.main import search_tags

        add_topic(title="SearchTagsActiveTopic", description="Desc", tags=["domain:main-searchtag-active"])

        result = search_tags("main-searchtag-active")
        assert "error" not in result
        entry = next(t for t in result["tags"] if t["tag"] == "domain:main-searchtag-active")
        assert entry["archived"] is False
        assert entry["archived_reason"] is None


class TestAnalyzeTagsOrphansArchivedField:
    def test_orphan_archived_field_present(self, temp_db):
        from src.main import analyze_tags

        # usage_count=1（デフォルトmin_usage=2未満）で孤児判定させる
        add_topic(
            title="AnalyzeTagsOrphanTopic", description="Desc",
            tags=["domain:main-orphan-legacy"],
        )
        _archive("domain:main-orphan-legacy")

        result = analyze_tags(include_domain_tags=True)
        assert "error" not in result
        orphan = next(
            (o for o in result["orphans"] if o["tag"] == "domain:main-orphan-legacy"), None
        )
        assert orphan is not None
        assert orphan["archived"] is True
        assert orphan["archived_reason"] == "退役済み"


# ========================================
# update_tag MCPツールのarchived/archived_reason受け渡し
# ========================================


class TestUpdateTagToolArchivedArgs:
    def test_archived_true_via_tool(self, temp_db):
        from src.main import update_tag

        add_topic(title="ToolArchiveTopic", description="Desc", tags=["domain:main-tool-archive"])

        result = update_tag("domain:main-tool-archive", archived=True, archived_reason="ツール経由の理由")
        assert "error" not in result
        assert result["archived"] is True
        assert result["archived_reason"] == "ツール経由の理由"

    def test_archived_false_via_tool(self, temp_db):
        from src.main import update_tag

        add_topic(title="ToolUnarchiveTopic", description="Desc", tags=["domain:main-tool-unarchive"])
        update_tag("domain:main-tool-unarchive", archived=True, archived_reason="理由")

        result = update_tag("domain:main-tool-unarchive", archived=False)
        assert "error" not in result
        assert result["archived"] is False


# ========================================
# search の archived_tags トップレベル集約
# ========================================


class TestSearchArchivedTagsSummary:
    """main.search() 応答トップレベルの archived_tags 集約（get系5関数と同様の集約。
    item単位のarchived/archived_tags/archived_factorはsearch_service._apply_archived_demotion
    が別途付与する）
    """

    def test_archived_tag_present_in_summary(self, temp_db, monkeypatch):
        from src.main import search
        import src.services.embedding_service as emb

        monkeypatch.setattr(emb, "_server_initialized", False)
        monkeypatch.setattr(emb, "_backfill_done", True)
        monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)

        topic = add_topic(
            title="SearchSummaryArchivedTopic", description="desc",
            tags=["domain:main-search-summary-legacy"],
        )
        add_decision(
            decision="SearchSummaryUniqueKeyword",
            reason="検索トップレベル集約テスト用",
            topic_id=topic["topic_id"],
            tags=["domain:main-search-summary-legacy"],
        )
        _archive("domain:main-search-summary-legacy")

        result = search(keyword="SearchSummaryUniqueKeyword")
        assert "error" not in result
        tags_seen = {a["tag"] for a in result["archived_tags"]}
        assert "domain:main-search-summary-legacy" in tags_seen

    def test_no_archived_tags_returns_empty_list(self, temp_db, monkeypatch):
        from src.main import search
        import src.services.embedding_service as emb

        monkeypatch.setattr(emb, "_server_initialized", False)
        monkeypatch.setattr(emb, "_backfill_done", True)
        monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)

        topic = add_topic(
            title="SearchSummaryActiveTopic", description="desc",
            tags=["domain:main-search-summary-active"],
        )
        add_decision(
            decision="SearchSummaryActiveUniqueKeyword",
            reason="検索トップレベル集約テスト用（非archived）",
            topic_id=topic["topic_id"],
            tags=["domain:main-search-summary-active"],
        )

        result = search(keyword="SearchSummaryActiveUniqueKeyword")
        assert "error" not in result
        assert result["archived_tags"] == []
