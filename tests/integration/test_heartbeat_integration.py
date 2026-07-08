"""ハートビート機能の統合テスト

get_activities の is_heartbeat_active, _build_activities_section の別セッション表示
"""
import os
import tempfile

import pytest

from src.db import init_database, get_connection
from src.services.activity_service import (
    add_activity,
    get_activities,
    update_activity,
    get_active_activities_by_tag,
)
from src.services.tag_service import _injected_tags
from src.services.pin_service import add_pin
from hooks.session_start_hook import _build_activities_section
from src.services.topic_service import add_topic
import src.services.embedding_service as emb


DEFAULT_TAGS = ["domain:test"]


def _build_activities_section_wrapper():
    """テスト用: connを自動管理してアクティビティセクションを組み立てる"""
    conn = get_connection()
    try:
        return _build_activities_section(conn)
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)


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


def _get_tag_id(namespace: str, name: str) -> int:
    """テスト用: タグIDを取得する"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM tags WHERE namespace = ? AND name = ?",
            (namespace, name),
        ).fetchone()
        return row["id"] if row else -1
    finally:
        conn.close()


# ========================================
# get_activities: is_heartbeat_active
# ========================================


class TestGetActivitiesHeartbeat:
    def test_is_heartbeat_active_false_by_default(self, temp_db):
        """last_heartbeat_atがNULLの場合、is_heartbeat_active=False"""
        add_activity(
            title="Activity", description="Desc", tags=DEFAULT_TAGS, check_in=False,
        )
        result = get_activities(tags=DEFAULT_TAGS)

        assert "error" not in result
        assert len(result["activities"]) == 1
        assert result["activities"][0]["is_heartbeat_active"] is False

    def test_is_heartbeat_active_true_when_recent(self, temp_db):
        """last_heartbeat_atが20分以内ならis_heartbeat_active=True"""
        activity = add_activity(
            title="Active HB", description="Desc", tags=DEFAULT_TAGS, check_in=False,
        )
        aid = activity["activity_id"]

        # last_heartbeat_atを現在時刻に設定
        conn = get_connection()
        conn.execute(
            "UPDATE activities SET last_heartbeat_at = datetime('now') WHERE id = ?",
            (aid,),
        )
        conn.commit()
        conn.close()

        result = get_activities(tags=DEFAULT_TAGS)

        assert "error" not in result
        assert result["activities"][0]["is_heartbeat_active"] is True

    def test_is_heartbeat_active_false_when_expired(self, temp_db):
        """last_heartbeat_atが20分以上前ならis_heartbeat_active=False"""
        activity = add_activity(
            title="Expired HB", description="Desc", tags=DEFAULT_TAGS, check_in=False,
        )
        aid = activity["activity_id"]

        # last_heartbeat_atを30分前に設定
        conn = get_connection()
        conn.execute(
            "UPDATE activities SET last_heartbeat_at = datetime('now', '-30 minutes') WHERE id = ?",
            (aid,),
        )
        conn.commit()
        conn.close()

        result = get_activities(tags=DEFAULT_TAGS)

        assert "error" not in result
        assert result["activities"][0]["is_heartbeat_active"] is False

    def test_is_heartbeat_active_field_is_bool(self, temp_db):
        """is_heartbeat_activeがbool型で返る"""
        add_activity(
            title="Activity", description="Desc", tags=DEFAULT_TAGS, check_in=False,
        )
        result = get_activities(tags=DEFAULT_TAGS)

        assert "error" not in result
        assert isinstance(result["activities"][0]["is_heartbeat_active"], bool)


# ========================================
# get_active_activities_by_tag: is_heartbeat_active
# ========================================


class TestGetActiveActivitiesByTagHeartbeat:
    def test_is_heartbeat_active_in_result(self, temp_db):
        """get_active_activities_by_tagの結果にis_heartbeat_activeが含まれる"""
        add_activity(
            title="Activity", description="Desc", tags=["domain:hb-test"], check_in=False,
        )
        tag_id = _get_tag_id("domain", "hb-test")
        activities = get_active_activities_by_tag(tag_id)

        assert len(activities) == 1
        assert "is_heartbeat_active" in activities[0]
        assert activities[0]["is_heartbeat_active"] is False

    def test_heartbeat_active_true(self, temp_db):
        """heartbeatが活性な場合、is_heartbeat_active=True"""
        activity = add_activity(
            title="Active", description="Desc", tags=["domain:hb-test"], check_in=False,
        )
        aid = activity["activity_id"]

        conn = get_connection()
        conn.execute(
            "UPDATE activities SET last_heartbeat_at = datetime('now') WHERE id = ?",
            (aid,),
        )
        conn.commit()
        conn.close()

        tag_id = _get_tag_id("domain", "hb-test")
        activities = get_active_activities_by_tag(tag_id)

        assert activities[0]["is_heartbeat_active"] is True


# ========================================
# _build_activities_section: 別セッション表示
# ========================================


class TestBuildActiveContextHeartbeat:
    def test_heartbeat_active_separate_section(self, temp_db):
        """heartbeat活性アクティビティが「作業中（別セッション）」セクションに表示される"""
        add_topic(title="Topic", description="Desc", tags=["domain:hb-ctx"])
        activity = add_activity(
            title="[作業] HB機能実装", description="Desc", tags=["domain:hb-ctx"], check_in=False,
        )
        aid = activity["activity_id"]
        update_activity(aid, status="in_progress")

        # heartbeatを活性化
        conn = get_connection()
        conn.execute(
            "UPDATE activities SET last_heartbeat_at = datetime('now') WHERE id = ?",
            (aid,),
        )
        conn.commit()
        conn.close()

        result = _build_activities_section_wrapper()

        assert "## 作業中（別セッション）" in result
        assert "[作業] HB機能実装" in result

    def test_normal_activity_in_hot_section(self, temp_db):
        """heartbeat非活性アクティビティは●/○マーカーで表示

        階層3・4は統計行に縮退するため、pending活動を個別行として検証するには
        pinで階層2（優先）に載せる必要がある。
        """
        add_topic(title="Topic", description="Desc", tags=["domain:hb-ctx2"])
        activity = add_activity(
            title="[作業] 通常タスク", description="Desc", tags=["domain:hb-ctx2"], check_in=False,
        )
        add_pin("tag", "domain:hb-ctx2", "activity", activity["activity_id"])

        result = _build_activities_section_wrapper()

        assert "○" in result
        assert "[作業] 通常タスク" in result
        assert "## 作業中（別セッション）" not in result

    def test_mixed_heartbeat_and_normal(self, temp_db):
        """heartbeat活性と非活性が混在する場合、両セクションに分離される

        階層3・4は統計行に縮退するため、非活性側のpending活動を個別行として
        検証するにはpinで階層2（優先）に載せる必要がある。
        """
        add_topic(title="Topic", description="Desc", tags=["domain:hb-mix"])

        hb_activity = add_activity(
            title="[作業] HB活性", description="Desc", tags=["domain:hb-mix"], check_in=False,
        )
        hb_aid = hb_activity["activity_id"]
        update_activity(hb_aid, status="in_progress")

        normal_activity = add_activity(
            title="[作業] 通常", description="Desc", tags=["domain:hb-mix"], check_in=False,
        )
        add_pin("tag", "domain:hb-mix", "activity", normal_activity["activity_id"])

        # HB活性化
        conn = get_connection()
        conn.execute(
            "UPDATE activities SET last_heartbeat_at = datetime('now') WHERE id = ?",
            (hb_aid,),
        )
        conn.commit()
        conn.close()

        result = _build_activities_section_wrapper()

        assert "## 作業中（別セッション）" in result
        assert "○" in result
        assert "[作業] HB活性" in result
        assert "[作業] 通常" in result

    def test_expired_heartbeat_in_normal_section(self, temp_db):
        """heartbeat期限切れアクティビティは通常セクションに表示

        階層3・4は統計行に縮退するため、pending活動を個別行として検証するには
        pinで階層2（優先）に載せる必要がある。
        """
        add_topic(title="Topic", description="Desc", tags=["domain:hb-exp"])
        activity = add_activity(
            title="[作業] 期限切れHB", description="Desc", tags=["domain:hb-exp"], check_in=False,
        )
        aid = activity["activity_id"]
        add_pin("tag", "domain:hb-exp", "activity", aid)

        # 30分前にheartbeat更新
        conn = get_connection()
        conn.execute(
            "UPDATE activities SET last_heartbeat_at = datetime('now', '-30 minutes') WHERE id = ?",
            (aid,),
        )
        conn.commit()
        conn.close()

        result = _build_activities_section_wrapper()

        assert "○" in result
        assert "## 作業中（別セッション）" not in result
        assert "[作業] 期限切れHB" in result
