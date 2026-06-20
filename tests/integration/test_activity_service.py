"""アクティビティサービスの統合テスト"""
import os
import tempfile
import pytest
from src.db import init_database, get_connection
from src.services.activity_service import add_activity, get_activities, update_activity
from src.services.tag_service import _injected_tags
from src.services.activity_service import add_activity, get_activities, update_activity, ACTIVITY_DESC_MAX_LEN


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        # tag_notes注入済みセットをリセット（テスト間の干渉防止）
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def activity_with_db(temp_db):
    """アクティビティを作成するフィクスチャ"""
    activity = add_activity(
        title="Test Activity",
        description="This is a test activity",
        tags=DEFAULT_TAGS,
        check_in=False,
    )
    return {"activity": activity}


class TestAddActivity:
    """add_activityの統合テスト"""

    def test_add_activity_success(self, temp_db):
        """アクティビティの追加が成功する（check_in=False）"""
        result = add_activity(
            title="New Activity",
            description="Activity description",
            tags=DEFAULT_TAGS,
            check_in=False,
        )

        assert "error" not in result
        assert result["activity_id"] > 0
        assert "check_in_result" not in result
        assert "available_intents" in result

    def test_add_activity_tags_required(self, temp_db):
        """tags=[]でTAGS_REQUIREDエラーになる"""
        result = add_activity(
            title="Activity",
            description="Description",
            tags=[],
        )

        assert "error" in result
        assert result["error"]["code"] == "TAGS_REQUIRED"

    def test_add_activity_tags_stored(self, temp_db):
        """アクティビティ作成時にactivity_tagsにレコードが正しくINSERTされる"""
        result = add_activity(
            title="Tagged Activity",
            description="Tagged description",
            tags=["domain:cc-memory", "hooks"],
            check_in=False,
        )

        assert "error" not in result
        assert result["activity_id"] > 0

        # DBで直接確認
        conn = get_connection()
        try:
            rows = conn.execute(
                """
                SELECT t.namespace, t.name
                FROM tags t
                JOIN activity_tags at ON t.id = at.tag_id
                WHERE at.activity_id = ?
                ORDER BY t.namespace, t.name
                """,
                (result["activity_id"],),
            ).fetchall()
            tag_names = sorted(f"{r['namespace']}:{r['name']}" if r['namespace'] else r['name'] for r in rows)
            assert tag_names == ["domain:cc-memory", "hooks"]
        finally:
            conn.close()

    def test_add_activity_with_check_in_default(self, temp_db):
        """デフォルト（check_in=True）でcheck_in_resultが含まれる"""
        result = add_activity(
            title="Activity with check-in",
            description="Description",
            tags=DEFAULT_TAGS,
        )

        assert "error" not in result
        assert "check_in_result" in result
        check_in_result = result["check_in_result"]
        assert "error" not in check_in_result
        assert "activity" in check_in_result
        assert check_in_result["activity"]["status"] == "in_progress"
        assert "tag_notes" in check_in_result
        assert "summary" in check_in_result

    def test_add_activity_with_check_in_false(self, temp_db):
        """check_in=Falseで従来動作（ステータスpending、check_in_resultなし）"""
        result = add_activity(
            title="Activity no check-in",
            description="Description",
            tags=DEFAULT_TAGS,
            check_in=False,
        )

        assert "error" not in result
        assert "check_in_result" not in result

    def test_add_activity_with_related(self, temp_db):
        """related指定でアクティビティ作成時にリレーションが張られる"""
        # トピックを作成
        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("Test Topic", "Topic description"),
            )
            topic_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        result = add_activity(
            title="Activity with topic",
            description="Description",
            tags=DEFAULT_TAGS,
            related=[{"type": "topic", "ids": [topic_id]}],
            check_in=False,
        )

        assert "error" not in result
        assert result["activity_id"] > 0

        # relationsテーブルに正規化されて格納されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM relations WHERE source_type = 'activity' AND source_id = ? AND target_type = 'topic' AND target_id = ?",
                (result["activity_id"], topic_id),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_add_activity_without_related(self, temp_db):
        """related未指定でリレーションなしのアクティビティが作成される"""
        result = add_activity(
            title="Activity no topic",
            description="Description",
            tags=DEFAULT_TAGS,
            check_in=False,
        )

        assert "error" not in result

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM relations WHERE source_type = 'activity' AND source_id = ?",
                (result["activity_id"],),
            ).fetchone()
            assert row["cnt"] == 0
        finally:
            conn.close()

    def test_add_activity_with_related_and_check_in(self, temp_db):
        """related + check_in=Trueでrelated_topicsが取得される"""
        # トピックを作成
        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("Test Topic", "Topic description"),
            )
            topic_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        result = add_activity(
            title="Activity with topic and check-in",
            description="Description",
            tags=DEFAULT_TAGS,
            related=[{"type": "topic", "ids": [topic_id]}],
        )

        assert "error" not in result
        assert "check_in_result" in result
        check_in_result = result["check_in_result"]
        assert "error" not in check_in_result
        # related_topicsに関連トピックが含まれる
        assert "related_topics" in check_in_result
        # α化: id_raw が元の整数 ID
        assert any(t["id_raw"] == topic_id for t in check_in_result["related_topics"])
        assert "recent_decisions" in check_in_result

    def test_add_activity_available_intents_format(self, temp_db):
        """available_intentsの各要素に"tag"と"description"がある"""
        result = add_activity(
            title="Activity",
            description="Desc",
            tags=DEFAULT_TAGS,
            check_in=False,
        )

        assert "error" not in result
        assert "available_intents" in result
        for intent in result["available_intents"]:
            assert "tag" in intent
            assert "description" in intent
            assert intent["tag"].startswith("intent:")

    def test_add_activity_available_intents_alphabetical(self, temp_db):
        """available_intentsがアルファベット順"""
        result = add_activity(
            title="Activity",
            description="Desc",
            tags=DEFAULT_TAGS,
            check_in=False,
        )

        assert "error" not in result
        intent_names = [i["tag"] for i in result["available_intents"]]
        assert intent_names == sorted(intent_names)

    def test_add_activity_available_intents_excludes_canonical(self, temp_db):
        """canonical_id持ちのintentはavailable_intentsから除外される"""
        from src.services.tag_service import update_tag

        # intent:debugとintent:investigateを作成するためにactivityを作成
        add_activity(
            title="Setup",
            description="Desc",
            tags=["domain:test", "intent:investigate"],
            check_in=False,
        )

        # intent:testaliasを作ってintent:investigateのエイリアスにする
        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO tags (namespace, name) VALUES ('intent', 'testalias')",
            )
            conn.commit()
        finally:
            conn.close()

        update_tag("intent:testalias", canonical="intent:investigate")

        result = add_activity(
            title="Activity",
            description="Desc",
            tags=DEFAULT_TAGS,
            check_in=False,
        )

        assert "error" not in result
        intent_tags = [i["tag"] for i in result["available_intents"]]
        assert "intent:testalias" not in intent_tags


class TestGetActivities:
    """get_activitiesの統合テスト"""

    def test_get_activities_invalid_limit_zero(self, temp_db):
        """limit=0でINVALID_PARAMETERエラーになる"""
        result = get_activities(tags=DEFAULT_TAGS, status="pending", limit=0)

        assert "error" in result
        assert result["error"]["code"] == "INVALID_PARAMETER"

    def test_get_activities_invalid_limit_negative(self, temp_db):
        """limit=-1でINVALID_PARAMETERエラーになる"""
        result = get_activities(tags=DEFAULT_TAGS, status="pending", limit=-1)

        assert "error" in result
        assert result["error"]["code"] == "INVALID_PARAMETER"

    def test_get_activities_invalid_status(self, temp_db):
        """無効なstatusでINVALID_STATUSエラーになる"""
        result = get_activities(tags=DEFAULT_TAGS, status="invalid_status")

        assert "error" in result
        assert result["error"]["code"] == "INVALID_STATUS"

    def test_get_activities_no_tags_returns_all(self, temp_db):
        """tags未指定で全アクティビティを返す"""
        add_activity(title="Activity A", description="Desc A", tags=["domain:test"], check_in=False)
        add_activity(title="Activity B", description="Desc B", tags=["domain:other"], check_in=False)

        result = get_activities()

        assert "error" not in result
        assert result["total_count"] == 2
        titles = {t["title"] for t in result["activities"]}
        assert titles == {"Activity A", "Activity B"}

    def test_get_activities_no_tags_with_status_filter(self, temp_db):
        """tags未指定 + status指定で全ドメインからフィルタ"""
        activity_a = add_activity(title="Activity A", description="Desc", tags=["domain:test"], check_in=False)
        add_activity(title="Activity B", description="Desc", tags=["domain:other"], check_in=False)
        update_activity(activity_a["activity_id"], status="completed")

        result = get_activities(status="completed")

        assert "error" not in result
        assert result["total_count"] == 1
        assert result["activities"][0]["title"] == "Activity A"

    def test_get_activities_completed_sorted_by_updated_at_desc(self, temp_db):
        """completedのソート順がupdated_at DESCになっている"""
        a1 = add_activity(title="Old completed", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        a2 = add_activity(title="New completed", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(a1["activity_id"], status="completed")
        update_activity(a2["activity_id"], status="completed")

        # a1のupdated_atを古い値に書き換えてソート順を明確にする
        conn = get_connection()
        conn.execute(
            "UPDATE activities SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
            (a1["activity_id"],),
        )
        conn.commit()
        conn.close()

        result = get_activities(status="completed", limit=10)

        assert "error" not in result
        assert result["total_count"] == 2
        titles = [a["title"] for a in result["activities"]]
        assert titles == ["New completed", "Old completed"]

    def test_get_activities_pending_sorted_by_updated_at_desc(self, temp_db):
        """pendingのソート順がupdated_at DESCになっている"""
        a1 = add_activity(title="Old pending", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        a2 = add_activity(title="New pending", description="Desc", tags=DEFAULT_TAGS, check_in=False)

        # a1のupdated_atを古い値に書き換えてソート順を明確にする
        conn = get_connection()
        conn.execute(
            "UPDATE activities SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
            (a1["activity_id"],),
        )
        conn.commit()
        conn.close()

        result = get_activities(status="pending", limit=10)

        assert "error" not in result
        assert result["total_count"] == 2
        titles = [a["title"] for a in result["activities"]]
        assert titles == ["New pending", "Old pending"]

    def test_get_activities_since_filter(self, temp_db):
        """since指定でupdated_at以降のアクティビティのみ返す"""
        add_activity(title="Old Activity", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_activity(title="New Activity", description="Desc", tags=DEFAULT_TAGS, check_in=False)

        conn = get_connection()
        conn.execute(
            "UPDATE activities SET updated_at = '2026-01-01 00:00:00' WHERE title = 'Old Activity'"
        )
        conn.execute(
            "UPDATE activities SET updated_at = '2026-03-15 00:00:00' WHERE title = 'New Activity'"
        )
        conn.commit()
        conn.close()

        result = get_activities(tags=DEFAULT_TAGS, since="2026-03-01")

        assert "error" not in result
        assert result["total_count"] == 1
        assert result["activities"][0]["title"] == "New Activity"

    def test_get_activities_until_filter(self, temp_db):
        """until指定でupdated_at以前のアクティビティのみ返す"""
        add_activity(title="Old Activity", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_activity(title="New Activity", description="Desc", tags=DEFAULT_TAGS, check_in=False)

        conn = get_connection()
        conn.execute(
            "UPDATE activities SET updated_at = '2026-01-01 00:00:00' WHERE title = 'Old Activity'"
        )
        conn.execute(
            "UPDATE activities SET updated_at = '2026-03-15 00:00:00' WHERE title = 'New Activity'"
        )
        conn.commit()
        conn.close()

        result = get_activities(tags=DEFAULT_TAGS, until="2026-02-01")

        assert "error" not in result
        assert result["total_count"] == 1
        assert result["activities"][0]["title"] == "Old Activity"

    def test_get_activities_since_and_until_combined(self, temp_db):
        """since+until指定で範囲内のアクティビティのみ返す"""
        add_activity(title="Jan", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_activity(title="Feb", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_activity(title="Mar", description="Desc", tags=DEFAULT_TAGS, check_in=False)

        conn = get_connection()
        conn.execute("UPDATE activities SET updated_at = '2026-01-15 00:00:00' WHERE title = 'Jan'")
        conn.execute("UPDATE activities SET updated_at = '2026-02-15 00:00:00' WHERE title = 'Feb'")
        conn.execute("UPDATE activities SET updated_at = '2026-03-15 00:00:00' WHERE title = 'Mar'")
        conn.commit()
        conn.close()

        result = get_activities(tags=DEFAULT_TAGS, since="2026-02-01", until="2026-02-28")

        assert "error" not in result
        assert result["total_count"] == 1
        assert result["activities"][0]["title"] == "Feb"

    def test_get_activities_since_with_status_filter(self, temp_db):
        """since + status組み合わせ"""
        a1 = add_activity(title="Old Completed", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        a2 = add_activity(title="New Pending", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(a1["activity_id"], status="completed")

        # update_activityが内部でupdated_at=CURRENT_TIMESTAMPを設定するため、
        # テスト用に手動で上書きしてsince条件の検証を可能にする
        conn = get_connection()
        conn.execute(
            "UPDATE activities SET updated_at = '2026-01-01 00:00:00' WHERE id = ?",
            (a1["activity_id"],),
        )
        conn.execute(
            "UPDATE activities SET updated_at = '2026-03-15 00:00:00' WHERE id = ?",
            (a2["activity_id"],),
        )
        conn.commit()
        conn.close()

        result = get_activities(status="completed", since="2026-03-01")

        assert "error" not in result
        assert result["total_count"] == 0
        assert result["activities"] == []

    def test_get_activities_until_includes_same_day(self, temp_db):
        """until指定日と同日のレコードが含まれる（境界テスト）"""
        add_activity(title="Same Day", description="Desc", tags=DEFAULT_TAGS, check_in=False)

        conn = get_connection()
        conn.execute(
            "UPDATE activities SET updated_at = '2026-03-15 18:00:00' WHERE title = 'Same Day'"
        )
        conn.commit()
        conn.close()

        result = get_activities(tags=DEFAULT_TAGS, until="2026-03-15")

        assert "error" not in result
        assert result["total_count"] == 1
        assert result["activities"][0]["title"] == "Same Day"

    def test_get_activities_invalid_since_format(self, temp_db):
        """不正なsince形式でINVALID_PARAMETERエラー"""
        result = get_activities(tags=DEFAULT_TAGS, since="not-a-date")

        assert "error" in result
        assert result["error"]["code"] == "INVALID_PARAMETER"

    def test_get_activities_invalid_until_format(self, temp_db):
        """不正なuntil形式でINVALID_PARAMETERエラー"""
        result = get_activities(tags=DEFAULT_TAGS, until="2026/03/15")

        assert "error" in result
        assert result["error"]["code"] == "INVALID_PARAMETER"

    def test_get_activities_truncates_description_at_max_len(self, temp_db):
        """descriptionがACTIVITY_DESC_MAX_LEN文字に切り詰められること"""
        long_desc = "a" * (ACTIVITY_DESC_MAX_LEN + 50)
        add_activity(title="Long Desc", description=long_desc, tags=["domain:test"])

        result = get_activities(tags=["domain:test"])

        assert "error" not in result
        activity = result["activities"][0]
        assert len(activity["description"]) == ACTIVITY_DESC_MAX_LEN


class TestUpdateActivity:
    """update_activityの統合テスト"""

    def test_update_status_to_in_progress(self, activity_with_db):
        """ステータスをin_progressに更新できる"""
        activity = activity_with_db["activity"]
        result = update_activity(activity["activity_id"], status="in_progress")

        assert "error" not in result
        assert result["status"] == "in_progress"

    def test_update_status_to_completed(self, activity_with_db):
        """ステータスをcompletedに更新できる"""
        activity = activity_with_db["activity"]
        result = update_activity(activity["activity_id"], status="completed")

        assert "error" not in result
        assert result["status"] == "completed"

    def test_update_status_invalid(self, activity_with_db):
        """無効なステータスでエラーになる"""
        activity = activity_with_db["activity"]
        result = update_activity(activity["activity_id"], status="invalid_status")

        assert "error" in result
        assert result["error"]["code"] == "INVALID_STATUS"

    def test_update_activity_not_found(self, temp_db):
        """存在しないアクティビティIDでエラーになる"""
        result = update_activity(9999, status="in_progress")

        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"


class TestSnoozedStatus:
    """snoozedステータスの統合テスト"""

    def test_set_status_to_snoozed(self, activity_with_db):
        """アクティビティをsnoozedに変更できる"""
        activity = activity_with_db["activity"]
        result = update_activity(activity["activity_id"], status="snoozed")

        assert "error" not in result
        assert result["status"] == "snoozed"

    def test_snoozed_excluded_from_active(self, temp_db):
        """snoozedアクティビティはstatus='active'に含まれない"""
        a = add_activity(title="Snoozed", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(a["activity_id"], status="snoozed")

        result = get_activities(status="active")

        assert "error" not in result
        assert result["total_count"] == 0

    def test_snoozed_returned_with_status_filter(self, temp_db):
        """get_activities(status='snoozed')でsnoozedアクティビティが返る"""
        a = add_activity(title="Snoozed", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(a["activity_id"], status="snoozed")

        result = get_activities(status="snoozed")

        assert "error" not in result
        assert result["total_count"] == 1
        assert result["activities"][0]["title"] == "Snoozed"

    def test_lazy_evaluation_restores_expired_snoozed(self, temp_db):
        """期限切れsnoozedがget_activities呼び出し時にpendingに復活する"""
        a = add_activity(title="Expired Snooze", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(a["activity_id"], status="snoozed")

        # updated_atを4日前に書き換えて期限切れにする
        conn = get_connection()
        conn.execute(
            "UPDATE activities SET updated_at = datetime('now', '-4 days') WHERE id = ?",
            (a["activity_id"],),
        )
        conn.commit()
        conn.close()

        # get_activitiesのlazy evaluationで復活するはず
        result = get_activities(status="pending")

        assert "error" not in result
        assert result["total_count"] == 1
        assert result["activities"][0]["title"] == "Expired Snooze"
        assert result["activities"][0]["status"] == "pending"

    def test_lazy_evaluation_keeps_unexpired_snoozed(self, temp_db):
        """期限内のsnoozedはget_activities呼び出し後もsnoozedのまま"""
        a = add_activity(title="Fresh Snooze", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(a["activity_id"], status="snoozed")

        # get_activitiesを呼んでもsnoozedのまま
        get_activities(status="active")
        result = get_activities(status="snoozed")

        assert "error" not in result
        assert result["total_count"] == 1
        assert result["activities"][0]["status"] == "snoozed"

    def test_field_update_wakes_snoozed(self, temp_db):
        """snoozed中にtitle更新すると自動的にpendingに復活する"""
        a = add_activity(title="Sleeping", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(a["activity_id"], status="snoozed")

        result = update_activity(a["activity_id"], title="Awake")

        assert "error" not in result
        assert result["status"] == "pending"

    def test_explicit_status_change_on_snoozed(self, temp_db):
        """snoozed→completedの明示的なステータス変更は尊重される"""
        a = add_activity(title="Done", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(a["activity_id"], status="snoozed")

        result = update_activity(a["activity_id"], status="completed")

        assert "error" not in result
        assert result["status"] == "completed"


class TestShelvedStatus:
    """shelvedステータスの統合テスト"""

    def test_set_status_to_shelved(self, activity_with_db):
        """アクティビティをshelvedに変更できる"""
        activity = activity_with_db["activity"]
        result = update_activity(activity["activity_id"], status="shelved")

        assert "error" not in result
        assert result["status"] == "shelved"

    def test_shelved_excluded_from_active(self, temp_db):
        """shelvedアクティビティはstatus='active'に含まれない"""
        a = add_activity(title="Shelved", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(a["activity_id"], status="shelved")

        result = get_activities(status="active")

        assert "error" not in result
        assert result["total_count"] == 0

    def test_shelved_returned_with_status_filter(self, temp_db):
        """get_activities(status='shelved')でshelvedアクティビティが返る"""
        a = add_activity(title="Shelved", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(a["activity_id"], status="shelved")

        result = get_activities(status="shelved")

        assert "error" not in result
        assert result["total_count"] == 1
        assert result["activities"][0]["title"] == "Shelved"

    def test_field_update_does_not_wake_shelved(self, temp_db):
        """shelved中にtitle更新してもshelvedのまま（snoozedとの差分）"""
        a = add_activity(title="On Shelf", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(a["activity_id"], status="shelved")

        result = update_activity(a["activity_id"], title="Still On Shelf")

        assert "error" not in result
        assert result["status"] == "shelved"
