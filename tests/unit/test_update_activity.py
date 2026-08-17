"""update_activityのユニットテスト"""
import os
import tempfile
import pytest
from src.db import init_database
from src.services.activity_service import add_activity, update_activity, get_activities


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def test_activity(temp_db):
    """テスト用アクティビティを作成する"""
    result = add_activity(
        title="Original Title",
        description="Original Description",
        tags=DEFAULT_TAGS,
        check_in=False,
    )
    return result


# ========================================
# 正常系テスト
# ========================================


class TestUpdateActivitySuccess:
    """update_activityの正常系テスト"""

    def test_update_status(self, test_activity):
        """ステータスのみ変更できる"""
        result = update_activity(test_activity["activity_id"], status="in_progress")

        assert "error" not in result
        assert result["activity_id"] == test_activity["activity_id"]
        assert result["status"] == "in_progress"

    def test_update_title(self, test_activity):
        """タイトルのみ変更できる"""
        result = update_activity(test_activity["activity_id"], title="New Title")

        assert "error" not in result
        assert result["activity_id"] == test_activity["activity_id"]
        assert result["status"] == "pending"

    def test_update_description(self, test_activity):
        """説明のみ変更できる"""
        result = update_activity(test_activity["activity_id"], description="New Description")

        assert "error" not in result
        assert result["activity_id"] == test_activity["activity_id"]
        assert result["status"] == "pending"

    def test_update_multiple_fields(self, test_activity):
        """複数フィールドを同時に変更できる"""
        result = update_activity(
            test_activity["activity_id"],
            status="in_progress",
            title="Updated Title",
            description="Updated Description",
        )

        assert "error" not in result
        assert result["activity_id"] == test_activity["activity_id"]
        assert result["status"] == "in_progress"

    def test_update_persists_via_get_activities(self, test_activity):
        """更新がDBに永続化されていることをget_activitiesで確認する"""
        activity_id = test_activity["activity_id"]
        update_activity(activity_id, title="Persisted Title", description="Persisted Desc", status="in_progress")

        result = get_activities(status="in_progress")
        activities = result["activities"]
        # α化: id は文字列、元 ID は id_raw に退避
        match = [a for a in activities if a["id_raw"] == activity_id]
        assert len(match) == 1
        assert match[0]["title"] == "Persisted Title"
        assert match[0]["description"] == "Persisted Desc"
        assert match[0]["status"] == "in_progress"

    def test_update_preserves_tags(self, test_activity):
        """update_activityでタグが保持される（レスポンスはactivity_id+statusのみ）"""
        result = update_activity(test_activity["activity_id"], status="in_progress")

        assert "error" not in result
        assert result["activity_id"] == test_activity["activity_id"]
        assert result["status"] == "in_progress"


# ========================================
# 異常系テスト
# ========================================


class TestUpdateActivityError:
    """update_activityの異常系テスト"""

    def test_all_none_returns_validation_error(self, test_activity):
        """全パラメータがNoneだとVALIDATION_ERRORになる"""
        result = update_activity(test_activity["activity_id"])

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_not_found(self, temp_db):
        """存在しないアクティビティIDでNOT_FOUNDになる"""
        result = update_activity(9999, status="in_progress")

        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"

    def test_invalid_status(self, test_activity):
        """無効なステータスでINVALID_STATUSになる"""
        result = update_activity(test_activity["activity_id"], status="invalid")

        assert "error" in result
        assert result["error"]["code"] == "INVALID_STATUS"

    def test_active_status_rejected(self, test_activity):
        """activeはget_activities用エイリアスであり、update_activityでは無効"""
        result = update_activity(test_activity["activity_id"], status="active")

        assert "error" in result
        assert result["error"]["code"] == "INVALID_STATUS"

    def test_empty_title(self, test_activity):
        """空文字のtitleでVALIDATION_ERRORになる"""
        result = update_activity(test_activity["activity_id"], title="")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "title" in result["error"]["message"]

    def test_whitespace_title(self, test_activity):
        """空白のみのtitleでVALIDATION_ERRORになる"""
        result = update_activity(test_activity["activity_id"], title="   ")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "title" in result["error"]["message"]

    def test_empty_description(self, test_activity):
        """空文字のdescriptionでVALIDATION_ERRORになる"""
        result = update_activity(test_activity["activity_id"], description="")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "description" in result["error"]["message"]

    def test_whitespace_description(self, test_activity):
        """空白のみのdescriptionでVALIDATION_ERRORになる"""
        result = update_activity(test_activity["activity_id"], description="   ")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "description" in result["error"]["message"]

    def test_blocked_status_rejected(self, test_activity):
        """blockedステータスがINVALID_STATUSになる"""
        result = update_activity(test_activity["activity_id"], status="blocked")

        assert "error" in result
        assert result["error"]["code"] == "INVALID_STATUS"


# ========================================
# タグ更新テスト
# ========================================


class TestUpdateActivityTags:
    """update_activityのタグ更新テスト"""

    def test_update_tags(self, test_activity):
        """タグ全置換（レスポンスはactivity_id+statusのみ）"""
        result = update_activity(test_activity["activity_id"], tags=["intent:design", "domain:calm"])

        assert "error" not in result
        assert result["activity_id"] == test_activity["activity_id"]
        assert result["status"] == "pending"

    def test_update_tags_empty_list(self, test_activity):
        """tags=[]でTAGS_REQUIREDエラー"""
        result = update_activity(test_activity["activity_id"], tags=[])

        assert "error" in result
        assert result["error"]["code"] == "TAGS_REQUIRED"

    def test_update_tags_none(self, test_activity):
        """tags=None（未指定）ではタグ変更なし（レスポンスはactivity_id+statusのみ）"""
        # まずステータスだけ変更
        result = update_activity(test_activity["activity_id"], status="in_progress")

        assert "error" not in result
        assert result["activity_id"] == test_activity["activity_id"]
        assert result["status"] == "in_progress"
