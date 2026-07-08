"""habit_serviceのユニットテスト"""
import os
import tempfile
import pytest
from src.db import get_connection, init_database
from src.services.habit_service import (
    add_habit,
    get_active_habit_contents_with_conn,
    get_habits,
    list_intelligently_habit_manifest_with_conn,
    update_habit,
)


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


class TestAddHabit:
    """add_habitのテスト"""

    def test_add_habit_success(self, temp_db):
        """振る舞いが正常に追加される"""
        result = add_habit("テスト振る舞い")

        assert "error" not in result
        assert result["habit_id"] is not None

    def test_add_habit_empty_content(self, temp_db):
        """空文字のcontentでバリデーションエラーになる"""
        result = add_habit("")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "content" in result["error"]["message"]

    def test_add_habit_whitespace_only(self, temp_db):
        """空白のみのcontentでバリデーションエラーになる"""
        result = add_habit("   ")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_add_multiple_habits(self, temp_db):
        """複数の振る舞いを追加できる"""
        result1 = add_habit("振る舞い1")
        result2 = add_habit("振る舞い2")

        assert "error" not in result1
        assert "error" not in result2
        assert result1["habit_id"] != result2["habit_id"]


class TestGetHabits:
    """get_habitsのテスト"""

    def test_get_habits_with_initial_data(self, temp_db):
        """マイグレーションで投入された初期データが含まれる"""
        result = get_habits()

        assert "error" not in result
        assert result["total_count"] >= 1
        # 初期データの内容を確認
        contents = [r["content"] for r in result["habits"]]
        assert any("IDを指示語代わりにしない" in c for c in contents)

    def test_get_habits_after_add(self, temp_db):
        """追加した振る舞いが一覧に含まれる"""
        add_habit("新しい振る舞い")

        result = get_habits()

        assert "error" not in result
        contents = [r["content"] for r in result["habits"]]
        assert "新しい振る舞い" in contents

    def test_get_habits_order_by_id(self, temp_db):
        """振る舞いがID順にソートされている"""
        add_habit("振る舞いA")
        add_habit("振る舞いB")

        result = get_habits()

        assert "error" not in result
        ids = [r["habit_id"] for r in result["habits"]]
        assert ids == sorted(ids)

    def test_get_habits_default_excludes_inactive(self, temp_db):
        """デフォルト（active省略）ではactive=1のみ返る"""
        created = add_habit("無効化される振る舞い")
        update_habit(created["habit_id"], active=False)
        add_habit("有効な振る舞い")

        result = get_habits()

        assert "error" not in result
        contents = [r["content"] for r in result["habits"]]
        assert "無効化される振る舞い" not in contents
        assert "有効な振る舞い" in contents
        assert all(r["active"] for r in result["habits"])

    def test_get_habits_active_false_returns_all(self, temp_db):
        """active=Falseを明示すると無効化済みも含めた全件が返る"""
        created = add_habit("無効化される振る舞い")
        update_habit(created["habit_id"], active=False)
        add_habit("有効な振る舞い")

        result = get_habits(active=False)

        assert "error" not in result
        contents = [r["content"] for r in result["habits"]]
        assert "無効化される振る舞い" in contents
        assert "有効な振る舞い" in contents

    def test_get_habits_active_true_explicit_same_as_default(self, temp_db):
        """active=Trueを明示指定してもデフォルトと同じ（active=1のみ）挙動になる"""
        created = add_habit("無効化される振る舞い")
        update_habit(created["habit_id"], active=False)
        add_habit("有効な振る舞い")

        result_default = get_habits()
        result_explicit = get_habits(active=True)

        assert result_default["total_count"] == result_explicit["total_count"]
        assert result_default["habits"] == result_explicit["habits"]
        habit_ids = {h["habit_id"] for h in result_default["habits"]}
        assert created["habit_id"] not in habit_ids


class TestUpdateHabit:
    """update_habitのテスト"""

    def test_update_content(self, temp_db):
        """contentを更新できる"""
        created = add_habit("元の振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, content="更新後の振る舞い")

        assert "error" not in result
        assert result["habit_id"] == habit_id
        assert result["content"] == "更新後の振る舞い"
        assert result["active"] == 1

    def test_update_active_to_false(self, temp_db):
        """active=Falseで無効化できる"""
        created = add_habit("無効化する振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, active=False)

        assert "error" not in result
        assert result["habit_id"] == habit_id
        assert result["active"] == 0

    def test_update_active_to_true(self, temp_db):
        """active=Trueで再有効化できる"""
        created = add_habit("再有効化する振る舞い")
        habit_id = created["habit_id"]
        update_habit(habit_id, active=False)

        result = update_habit(habit_id, active=True)

        assert "error" not in result
        assert result["active"] == 1

    def test_update_both_content_and_active(self, temp_db):
        """contentとactiveを同時に更新できる"""
        created = add_habit("元の振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, content="新しい振る舞い", active=False)

        assert "error" not in result
        assert result["content"] == "新しい振る舞い"
        assert result["active"] == 0

    def test_update_no_params(self, temp_db):
        """content/active両方未指定でバリデーションエラーになる"""
        result = update_habit(1)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "At least one" in result["error"]["message"]

    def test_update_not_found(self, temp_db):
        """存在しないIDでNOT_FOUNDエラーになる"""
        result = update_habit(9999, content="存在しない振る舞い")

        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"
        assert "9999" in result["error"]["message"]

    def test_update_empty_content(self, temp_db):
        """空文字のcontentでバリデーションエラーになる"""
        created = add_habit("元の振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, content="")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_update_invalid_active(self, temp_db):
        """非bool値でバリデーションエラーになる"""
        created = add_habit("元の振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, active=2)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "active must be True or False" in result["error"]["message"]


class TestTriggerModeSplit:
    """trigger_mode（always/intelligently）分割のテスト"""

    def test_new_habit_defaults_to_always(self, temp_db):
        """新規追加した振る舞いはtrigger_mode='always'になる"""
        created = add_habit("新規振る舞い")

        result = get_habits(habit_id=created["habit_id"])

        assert result["habits"][0]["trigger_mode"] == "always"

    def test_get_active_habit_contents_excludes_intelligently(self, temp_db):
        """get_active_habit_contents_with_connはintelligently層を含まない"""
        conn = get_connection()
        try:
            always_id = add_habit("always振る舞い")["habit_id"]
            intelligently_id = add_habit("intelligently振る舞い")["habit_id"]
            conn.execute(
                "UPDATE habits SET trigger_mode = 'intelligently' WHERE id = ?",
                (intelligently_id,),
            )
            conn.commit()

            contents = get_active_habit_contents_with_conn(conn)

            assert "always振る舞い" in contents
            assert "intelligently振る舞い" not in contents
        finally:
            conn.close()

    def test_manifest_lists_intelligently_only(self, temp_db):
        """list_intelligently_habit_manifest_with_connはintelligently層のみ返す"""
        conn = get_connection()
        try:
            always_id = add_habit("always振る舞い")["habit_id"]
            intelligently_id = add_habit("intelligently振る舞い")["habit_id"]
            conn.execute(
                "UPDATE habits SET trigger_mode = 'intelligently' WHERE id = ?",
                (intelligently_id,),
            )
            conn.commit()

            manifest = list_intelligently_habit_manifest_with_conn(conn)
            manifest_ids = [m["habit_id"] for m in manifest]

            assert intelligently_id in manifest_ids
            assert always_id not in manifest_ids
            entry = next(m for m in manifest if m["habit_id"] == intelligently_id)
            assert entry["trigger_mode"] == "intelligently"
            assert entry["title"] == "intelligently振る舞い"
        finally:
            conn.close()

    def test_manifest_title_uses_description_when_present(self, temp_db):
        """descriptionが設定されていればtitleにdescriptionを使う"""
        conn = get_connection()
        try:
            habit_id = add_habit("本文は長め" * 10)["habit_id"]
            conn.execute(
                "UPDATE habits SET trigger_mode = 'intelligently', description = ? WHERE id = ?",
                ("短い要旨", habit_id),
            )
            conn.commit()

            manifest = list_intelligently_habit_manifest_with_conn(conn)

            entry = next(m for m in manifest if m["habit_id"] == habit_id)
            assert entry["title"] == "短い要旨"
        finally:
            conn.close()

    def test_manifest_excludes_inactive(self, temp_db):
        """active=0のintelligently振る舞いはマニフェストに出ない"""
        conn = get_connection()
        try:
            created = add_habit("無効化されるintelligently")
            habit_id = created["habit_id"]
            conn.execute(
                "UPDATE habits SET trigger_mode = 'intelligently', active = 0 WHERE id = ?",
                (habit_id,),
            )
            conn.commit()

            manifest = list_intelligently_habit_manifest_with_conn(conn)

            assert habit_id not in [m["habit_id"] for m in manifest]
        finally:
            conn.close()

    def test_manifest_orders_by_importance_score_desc(self, temp_db):
        """importance_score降順（同値はid昇順）で並ぶ"""
        conn = get_connection()
        try:
            low_id = add_habit("低優先度")["habit_id"]
            high_id = add_habit("高優先度")["habit_id"]
            mid_id = add_habit("中優先度")["habit_id"]
            conn.executemany(
                "UPDATE habits SET trigger_mode = 'intelligently', importance_score = ? WHERE id = ?",
                [(0.5, low_id), (2.0, high_id), (1.0, mid_id)],
            )
            conn.commit()

            manifest = list_intelligently_habit_manifest_with_conn(conn)
            manifest_ids = [m["habit_id"] for m in manifest]

            assert manifest_ids == [high_id, mid_id, low_id]
        finally:
            conn.close()


class TestGetHabitsSingleFetch:
    """get_habitsのhabit_id単一取得と参照スタンプのテスト"""

    def test_habit_id_returns_single_habit(self, temp_db):
        """habit_id指定時はその1件のみが返る"""
        target = add_habit("対象の振る舞い")
        add_habit("別の振る舞い")

        result = get_habits(habit_id=target["habit_id"])

        assert result["total_count"] == 1
        assert result["habits"][0]["habit_id"] == target["habit_id"]
        assert result["habits"][0]["content"] == "対象の振る舞い"

    def test_habit_id_updates_last_recalled_at(self, temp_db):
        """habit_id指定での取得はlast_recalled_atを更新する"""
        created = add_habit("参照される振る舞い")
        habit_id = created["habit_id"]

        before = get_habits(active=False)
        before_stamp = next(
            h["last_recalled_at"] for h in before["habits"] if h["habit_id"] == habit_id
        )
        assert before_stamp is None

        get_habits(habit_id=habit_id)

        after = get_habits(active=False)
        after_stamp = next(
            h["last_recalled_at"] for h in after["habits"] if h["habit_id"] == habit_id
        )
        assert after_stamp is not None

    def test_habit_id_not_found_returns_empty(self, temp_db):
        """存在しないhabit_idを指定すると空一覧が返る（エラーにしない）"""
        result = get_habits(habit_id=9999)

        assert "error" not in result
        assert result["total_count"] == 0
        assert result["habits"] == []
