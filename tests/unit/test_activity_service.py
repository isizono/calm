"""activity_service の単体テスト

add_activityのpins引数（作成と同時にpinを張る機能）の挙動を検証する。
"""
import os
import tempfile
import pytest

from src.db import init_database, get_connection
from src.services.activity_service import add_activity
from src.services.material_service import add_material
from src.services.topic_service import add_topic
from src.services.tag_service import _injected_tags, ensure_tag_ids


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
def material(temp_db):
    """テスト用資材を作成する"""
    return add_material(
        title="テスト資材",
        content="テスト資材の内容",
        tags=DEFAULT_TAGS,
        source="テスト用データ",
    )


@pytest.fixture
def topic(temp_db):
    """テスト用トピックを作成する"""
    return add_topic(title="テストトピック", description="テスト用", tags=DEFAULT_TAGS)


def _pin_exists(activity_id: int, target_type: str, target_id: int) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM pins WHERE source_type='activity' AND source_id=? "
            "AND target_type=? AND target_id=?",
            (activity_id, target_type, target_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


class TestAddActivityPinsBasic:
    """pins引数: 作成と同時にpinを張る"""

    def test_pin_to_material_on_create(self, material):
        """materialへのpinを作成と同時に張れる"""
        material_id = material["material_id"]

        result = add_activity(
            title="テストactivity", description="説明", tags=DEFAULT_TAGS,
            pins=[{"type": "material", "ref": material_id}],
        )

        assert "error" not in result
        activity_id = result["activity_id"]
        assert _pin_exists(activity_id, "material", material_id)

    def test_pin_to_topic_on_create(self, topic):
        """topicへのpinを作成と同時に張れる"""
        topic_id = topic["topic_id"]

        result = add_activity(
            title="テストactivity", description="説明", tags=DEFAULT_TAGS,
            pins=[{"type": "topic", "ref": topic_id}],
        )

        assert "error" not in result
        activity_id = result["activity_id"]
        assert _pin_exists(activity_id, "topic", topic_id)

    def test_pin_to_tag_ref_string_on_create(self, temp_db):
        """tagのrefをnamespace:name形式の文字列で指定できる"""
        conn = get_connection()
        try:
            tag_ids = ensure_tag_ids(conn, [("domain", "cc-memory")])
            conn.commit()
            tag_id = tag_ids[0]
        finally:
            conn.close()

        result = add_activity(
            title="テストactivity", description="説明", tags=DEFAULT_TAGS,
            pins=[{"type": "tag", "ref": "domain:cc-memory"}],
        )

        assert "error" not in result
        activity_id = result["activity_id"]
        assert _pin_exists(activity_id, "tag", tag_id)

    def test_multiple_pins_on_create(self, material, topic):
        """複数のpinを同時に張れる"""
        material_id = material["material_id"]
        topic_id = topic["topic_id"]

        result = add_activity(
            title="テストactivity", description="説明", tags=DEFAULT_TAGS,
            pins=[
                {"type": "material", "ref": material_id},
                {"type": "topic", "ref": topic_id},
            ],
        )

        assert "error" not in result
        activity_id = result["activity_id"]
        assert _pin_exists(activity_id, "material", material_id)
        assert _pin_exists(activity_id, "topic", topic_id)


class TestAddActivityPinsFailure:
    """pins引数: 失敗時は全体を失敗させる（部分成功しない）"""

    def test_nonexistent_pin_target_fails_entire_creation(self, temp_db):
        """存在しないpin targetを指定するとactivity自体の作成も失敗する"""
        result = add_activity(
            title="テストactivity", description="説明", tags=DEFAULT_TAGS,
            pins=[{"type": "material", "ref": 99999}],
        )

        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM activities WHERE title = ?", ("テストactivity",)
            ).fetchone()
            assert row is None
        finally:
            conn.close()

    def test_invalid_pin_type_fails_before_activity_created(self, temp_db):
        """pinsのtypeが不正な場合、activity作成前にVALIDATION_ERRORで弾かれる"""
        result = add_activity(
            title="テストactivity", description="説明", tags=DEFAULT_TAGS,
            pins=[{"type": "invalid_type", "ref": 1}],
        )

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM activities WHERE title = ?", ("テストactivity",)
            ).fetchone()
            assert row is None
        finally:
            conn.close()

    def test_pin_missing_required_field_returns_validation_error(self, temp_db):
        """pinsの要素にrefが欠けている場合VALIDATION_ERRORを返す"""
        result = add_activity(
            title="テストactivity", description="説明", tags=DEFAULT_TAGS,
            pins=[{"type": "material"}],
        )

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestAddActivityPinsIdempotent:
    """pins引数: 冪等性"""

    def test_duplicate_pin_targets_in_same_call_is_idempotent(self, material):
        """同一pinsに同じtargetを重複指定してもエラーにならず1件のみ作成される"""
        material_id = material["material_id"]

        result = add_activity(
            title="テストactivity", description="説明", tags=DEFAULT_TAGS,
            pins=[
                {"type": "material", "ref": material_id},
                {"type": "material", "ref": material_id},
            ],
        )

        assert "error" not in result
        activity_id = result["activity_id"]

        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM pins WHERE source_type='activity' AND source_id=? "
                "AND target_type='material' AND target_id=?",
                (activity_id, material_id),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1


class TestAddActivityWithoutPins:
    """pins引数を省略した場合の後方互換性"""

    def test_no_pins_argument_creates_activity_without_pins(self, temp_db):
        """pinsを指定しない場合、activityは作成されpinsテーブルには何も追加されない"""
        result = add_activity(
            title="テストactivity", description="説明", tags=DEFAULT_TAGS,
        )

        assert "error" not in result
        activity_id = result["activity_id"]

        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM pins WHERE source_type='activity' AND source_id=?",
                (activity_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0
