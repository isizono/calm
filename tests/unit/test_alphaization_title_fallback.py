"""α化時の title fallback テスト (PR #405 review fix)

title が None の decision / log を α化すると `(#NNN)` のみになる問題を防ぐ。
本文先頭50文字を fallback として `{snippet} (#NNN)` 形式にする。

対応箇所:
- search_service._format_row (decision ブランチ) → get_by_id / get_by_ids 経由
- checkin_service._get_logs_catalog_from_topics → check_in の latest_log / logs カタログ
"""
import os
import tempfile

import pytest

from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.decision_service import add_decisions
from src.services.discussion_log_service import add_logs
from src.services.search_service import get_by_id, get_by_ids
from src.services.checkin_service import _get_logs_catalog_from_topics
from src.services.tag_service import _injected_tags


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def topic(temp_db):
    return add_topic(title="テストトピック", description="テスト用", tags=DEFAULT_TAGS)


class TestDecisionTitleFallbackInFormatRow:
    """_format_row decision ブランチで title None 時に decision 本文へ fallback する"""

    def test_get_by_id_uses_decision_body_when_title_none(self, topic):
        """get_by_id(decision) は title なしのとき decision 本文先頭50文字を表示する"""
        body = "これがdecision本文の中身でtitleは指定されていない"
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": body, "reason": "理由"},
        ])
        did = result["created"][0]["decision_id"]

        res = get_by_id("decision", did)
        assert "error" not in res
        data = res["data"]
        # title フィールドに本文 fallback が乗る
        assert data["title"] == body[:50]
        # α化された id 文字列にも fallback タイトルが組み込まれる
        assert data["id"] == f"{body[:50]} (#{did})"
        assert data["id_raw"] == did

    def test_get_by_ids_uses_decision_body_when_title_none(self, topic):
        """get_by_ids(decision) も同様に本文 fallback する"""
        body = "本文だけのdecision本体"
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": body, "reason": "理由"},
        ])
        did = result["created"][0]["decision_id"]

        res = get_by_ids([{"type": "decision", "id": did}])
        assert "error" not in res
        items = res["results"]
        assert len(items) == 1
        data = items[0]["data"]
        assert data["title"] == body
        assert data["id"] == f"{body} (#{did})"

    def test_get_by_id_prefers_title_when_provided(self, topic):
        """title 指定済みなら本文 fallback ではなく title が使われる"""
        result = add_decisions([
            {
                "topic_id": topic["topic_id"],
                "decision": "本文",
                "reason": "理由",
                "title": "正しい題名",
            },
        ])
        did = result["created"][0]["decision_id"]

        res = get_by_id("decision", did)
        data = res["data"]
        assert data["title"] == "正しい題名"
        assert data["id"] == f"正しい題名 (#{did})"


class TestLogsCatalogTitleFallback:
    """_get_logs_catalog_from_topics で title None 時に content へ fallback する"""

    def test_latest_log_falls_back_to_content_when_title_none(self, topic):
        """latest_log の title None は content 先頭50文字に fallback"""
        content = "titleなしlogの本文を50文字に切り詰めるサンプル"
        add_logs([
            {"topic_id": topic["topic_id"], "content": content},
        ])

        conn = get_connection()
        try:
            latest_log, catalog = _get_logs_catalog_from_topics(conn, [topic["topic_id"]])
        finally:
            conn.close()

        assert latest_log is not None
        log_id = latest_log["id_raw"]
        assert latest_log["title"] == content[:50]
        assert latest_log["id"] == f"{content[:50]} (#{log_id})"

    def test_catalog_falls_back_to_content_when_title_none(self, topic):
        """catalog の各 log も title None なら content 先頭50文字に fallback"""
        tid = topic["topic_id"]
        add_logs([
            {"topic_id": tid, "content": "旧log本文1"},
            {"topic_id": tid, "content": "旧log本文2"},
            {"topic_id": tid, "content": "最新log本文（latest側）"},
        ])

        conn = get_connection()
        try:
            latest_log, catalog = _get_logs_catalog_from_topics(conn, [tid])
        finally:
            conn.close()

        assert latest_log is not None
        # catalog には残り2件が新しい順に入る
        assert len(catalog) == 2
        for item in catalog:
            assert "id_raw" in item
            # title None だと "(#NNN)" のみになる挙動を回避できているか
            assert not item["id"].startswith("(#"), (
                f"catalog item id should embed content fallback, got {item['id']!r}"
            )

    def test_latest_log_prefers_title_when_provided(self, topic):
        """title 指定済みなら fallback されず title がそのまま使われる"""
        add_logs([
            {"topic_id": topic["topic_id"], "content": "本文", "title": "ログ題名"},
        ])

        conn = get_connection()
        try:
            latest_log, _catalog = _get_logs_catalog_from_topics(conn, [topic["topic_id"]])
        finally:
            conn.close()

        assert latest_log is not None
        log_id = latest_log["id_raw"]
        assert latest_log["title"] == "ログ題名"
        assert latest_log["id"] == f"ログ題名 (#{log_id})"
