"""habit_serviceのユニットテスト"""
import os
import tempfile
import pytest
from src.config import ALWAYS_POOL_CAPACITY
from src.db import get_connection, init_database
from src.services.habit_service import (
    _add_habit_with_conn,
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

    @pytest.mark.parametrize("importance_score", [0, 4, -1, 1.5])
    def test_add_habit_invalid_importance_score(self, temp_db, importance_score):
        """importance_scoreが1/2/3以外だとバリデーションエラーになる"""
        result = add_habit("不正なスコアの振る舞い", importance_score=importance_score)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "importance_score" in result["error"]["message"]

    @pytest.mark.parametrize("importance_score", [1, 2, 3])
    def test_add_habit_valid_importance_score(self, temp_db, importance_score):
        """importance_scoreが1/2/3ならエラーにならない"""
        result = add_habit("有効なスコアの振る舞い", importance_score=importance_score)

        assert "error" not in result

    def test_add_habit_invalid_status(self, temp_db):
        """statusがactive/archived以外だとバリデーションエラーになる"""
        result = add_habit("不正なstatusの振る舞い", status="deleted")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "status" in result["error"]["message"]

    def test_add_habit_status_archived(self, temp_db):
        """status='archived'を指定して追加できる"""
        result = add_habit("最初からアーカイブする振る舞い", status="archived")

        assert "error" not in result


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

    def test_update_trigger_mode_to_intelligently(self, temp_db):
        """trigger_mode='intelligently'に更新できる"""
        created = add_habit("intelligently化する振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, trigger_mode="intelligently")

        assert "error" not in result
        assert result["trigger_mode"] == "intelligently"

    def test_update_trigger_mode_invalid_value(self, temp_db):
        """trigger_modeが'always'/'intelligently'以外だとバリデーションエラーになる"""
        created = add_habit("元の振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, trigger_mode="sometimes")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "trigger_mode" in result["error"]["message"]

    def test_update_description(self, temp_db):
        """descriptionを更新できる"""
        created = add_habit("要旨をつける振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, description="短い要旨")

        assert "error" not in result
        assert result["description"] == "短い要旨"

    def test_update_trigger_mode_and_description_together(self, temp_db):
        """trigger_modeとdescriptionを同時に更新できる"""
        created = add_habit("まとめて更新する振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(
            habit_id, trigger_mode="intelligently", description="要旨テキスト"
        )

        assert "error" not in result
        assert result["trigger_mode"] == "intelligently"
        assert result["description"] == "要旨テキスト"

    def test_update_description_too_long(self, temp_db):
        """descriptionが100字を超えるとバリデーションエラーになる"""
        created = add_habit("要旨が長すぎる振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, description="あ" * 101)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "description" in result["error"]["message"]

    def test_update_description_exactly_max_length(self, temp_db):
        """descriptionがちょうど100字ならエラーにならない"""
        created = add_habit("要旨がちょうど上限の振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, description="あ" * 100)

        assert "error" not in result
        assert result["description"] == "あ" * 100

    @pytest.mark.parametrize("importance_score", [0, 4, -1])
    def test_update_invalid_importance_score(self, temp_db, importance_score):
        """importance_scoreが1/2/3以外だとバリデーションエラーになる"""
        created = add_habit("スコア更新対象の振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, importance_score=importance_score)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "importance_score" in result["error"]["message"]

    def test_update_importance_score_valid(self, temp_db):
        """importance_scoreを1/2/3に更新できる"""
        created = add_habit("スコア更新対象の振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, importance_score=1)

        assert "error" not in result
        assert result["importance_score"] == 1

    def test_update_invalid_status(self, temp_db):
        """statusがactive/archived以外だとバリデーションエラーになる"""
        created = add_habit("status更新対象の振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, status="deleted")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "status" in result["error"]["message"]

    def test_update_status_to_archived(self, temp_db):
        """status='archived'に更新できる"""
        created = add_habit("アーカイブする振る舞い")
        habit_id = created["habit_id"]

        result = update_habit(habit_id, status="archived")

        assert "error" not in result
        assert result["status"] == "archived"


class TestTriggerModeSplit:
    """trigger_mode（always/intelligently）分割のテスト"""

    def test_new_habit_defaults_to_intelligently(self, temp_db):
        """新規追加した振る舞いはtrigger_mode='intelligently'になる
        （常時注入層への自動着地を防ぐための既定値）"""
        created = add_habit("新規振る舞い")

        result = get_habits(habit_id=created["habit_id"])

        assert result["habits"][0]["trigger_mode"] == "intelligently"

    def test_get_active_habit_contents_excludes_intelligently(self, temp_db):
        """get_active_habit_contents_with_connはintelligently層を含まない"""
        conn = get_connection()
        try:
            always_id = add_habit("always振る舞い")["habit_id"]
            add_habit("intelligently振る舞い")
            conn.execute(
                "UPDATE habits SET trigger_mode = 'always' WHERE id = ?",
                (always_id,),
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
                "UPDATE habits SET trigger_mode = 'always' WHERE id = ?",
                (always_id,),
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

    def test_manifest_orders_by_importance_score_asc(self, temp_db):
        """importance_score昇順（同値はid昇順）で並び、1(critical)が先頭に出る"""
        conn = get_connection()
        try:
            low_id = add_habit("低優先度")["habit_id"]
            high_id = add_habit("高優先度")["habit_id"]
            mid_id = add_habit("中優先度")["habit_id"]
            conn.executemany(
                "UPDATE habits SET trigger_mode = 'intelligently', importance_score = ? WHERE id = ?",
                [(3, low_id), (1, high_id), (2, mid_id)],
            )
            conn.commit()

            manifest = list_intelligently_habit_manifest_with_conn(conn)
            manifest_ids = [m["habit_id"] for m in manifest]

            assert manifest_ids == [high_id, mid_id, low_id]
        finally:
            conn.close()

    def test_manifest_orders_by_id_when_importance_score_tied(self, temp_db):
        """importance_scoreが同値のときはid昇順で並ぶ"""
        conn = get_connection()
        try:
            first_id = add_habit("先に追加")["habit_id"]
            second_id = add_habit("後に追加")["habit_id"]
            conn.executemany(
                "UPDATE habits SET trigger_mode = 'intelligently', importance_score = ? WHERE id = ?",
                [(2, first_id), (2, second_id)],
            )
            conn.commit()

            manifest = list_intelligently_habit_manifest_with_conn(conn)
            manifest_ids = [m["habit_id"] for m in manifest]

            assert manifest_ids == [first_id, second_id]
        finally:
            conn.close()

    def test_manifest_includes_importance_label(self, temp_db):
        """importance_scoreからcritical/important/defaultラベルが導出される"""
        conn = get_connection()
        try:
            critical_id = add_habit("critical振る舞い")["habit_id"]
            important_id = add_habit("important振る舞い")["habit_id"]
            default_id = add_habit("default振る舞い")["habit_id"]
            conn.executemany(
                "UPDATE habits SET trigger_mode = 'intelligently', importance_score = ? WHERE id = ?",
                [(1, critical_id), (2, important_id), (3, default_id)],
            )
            conn.commit()

            manifest = list_intelligently_habit_manifest_with_conn(conn)
            labels = {m["habit_id"]: m["importance_label"] for m in manifest}

            assert labels[critical_id] == "critical"
            assert labels[important_id] == "important"
            assert labels[default_id] == "default"
        finally:
            conn.close()

    def test_manifest_excludes_archived_status(self, temp_db):
        """status='archived'の振る舞いはマニフェストから除外される"""
        conn = get_connection()
        try:
            archived_id = add_habit("アーカイブ済み")["habit_id"]
            active_id = add_habit("現役")["habit_id"]
            conn.execute(
                "UPDATE habits SET trigger_mode = 'intelligently', status = 'archived' WHERE id = ?",
                (archived_id,),
            )
            conn.execute(
                "UPDATE habits SET trigger_mode = 'intelligently' WHERE id = ?",
                (active_id,),
            )
            conn.commit()

            manifest = list_intelligently_habit_manifest_with_conn(conn)
            manifest_ids = [m["habit_id"] for m in manifest]

            assert archived_id not in manifest_ids
            assert active_id in manifest_ids
        finally:
            conn.close()


class TestManifestDecay:
    """list_intelligently_habit_manifest_with_connのレンダー時decayのテスト"""

    def test_old_habit_without_recall_excluded_from_manifest(self, temp_db):
        """90日超過+last_recalled_at無しのintelligently habitはマニフェストから除外される"""
        conn = get_connection()
        try:
            habit_id = add_habit("古い振る舞い")["habit_id"]
            conn.execute(
                "UPDATE habits SET trigger_mode = 'intelligently', "
                "created_at = datetime('now', '-91 days') WHERE id = ?",
                (habit_id,),
            )
            conn.commit()

            manifest = list_intelligently_habit_manifest_with_conn(conn)

            assert habit_id not in [m["habit_id"] for m in manifest]
        finally:
            conn.close()

    def test_old_habit_with_recent_recall_stays_in_manifest(self, temp_db):
        """90日超過でもlast_recalled_atが最近なら除外されない"""
        conn = get_connection()
        try:
            habit_id = add_habit("古いが最近参照された振る舞い")["habit_id"]
            conn.execute(
                "UPDATE habits SET trigger_mode = 'intelligently', "
                "created_at = datetime('now', '-91 days'), "
                "last_recalled_at = datetime('now', '-1 days') WHERE id = ?",
                (habit_id,),
            )
            conn.commit()

            manifest = list_intelligently_habit_manifest_with_conn(conn)

            assert habit_id in [m["habit_id"] for m in manifest]
        finally:
            conn.close()

    def test_recently_created_habit_without_recall_stays_in_manifest(self, temp_db):
        """作成が最近（90日以内）ならlast_recalled_at無しでも除外されない
        （作成直後にdecayさせないための境界確認）"""
        conn = get_connection()
        try:
            habit_id = add_habit("新規振る舞い")["habit_id"]
            conn.execute(
                "UPDATE habits SET trigger_mode = 'intelligently' WHERE id = ?",
                (habit_id,),
            )
            conn.commit()

            manifest = list_intelligently_habit_manifest_with_conn(conn)

            assert habit_id in [m["habit_id"] for m in manifest]
        finally:
            conn.close()

    def test_decayed_habit_still_returned_by_get_habits(self, temp_db):
        """マニフェストからdecay除外されたhabitも、get_habits(active=True)の全件listには出続ける"""
        conn = get_connection()
        try:
            habit_id = add_habit("decay対象だがget_habitsには出る")["habit_id"]
            conn.execute(
                "UPDATE habits SET trigger_mode = 'intelligently', "
                "created_at = datetime('now', '-91 days') WHERE id = ?",
                (habit_id,),
            )
            conn.commit()

            manifest = list_intelligently_habit_manifest_with_conn(conn)
            assert habit_id not in [m["habit_id"] for m in manifest]
        finally:
            conn.close()

        result = get_habits(active=True)
        assert habit_id in [h["habit_id"] for h in result["habits"]]


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


def _neutralize_seed_always_pool(conn) -> None:
    """migration由来の初期habit（trigger_mode='always'）をintelligently化し、
    alwaysプール合計をテストごとに0からの決定論的な値にする。"""
    conn.execute(
        "UPDATE habits SET trigger_mode = 'intelligently' WHERE trigger_mode = 'always'"
    )
    conn.commit()


def _make_always(conn, content: str) -> int:
    """ゲートを経由せず、指定contentの振る舞いを直接trigger_mode='always'にする。

    棚卸し未実施でプールが定員超過している現状データを模した fixture 用。
    """
    habit_id = add_habit(content)["habit_id"]
    conn.execute("UPDATE habits SET trigger_mode = 'always' WHERE id = ?", (habit_id,))
    conn.commit()
    return habit_id


class TestAlwaysPromotionGate:
    """update_habitのalways昇格ゲート（短さ検査+プール定員のラチェット検査）のテスト"""

    def test_add_habit_shared_entry_point_defaults_to_intelligently(self, temp_db):
        """add_habitとdecision propagateが共有する_add_habit_with_connも
        trigger_mode='intelligently'で作成する"""
        conn = get_connection()
        try:
            habit_id = _add_habit_with_conn(conn, "propagate経由相当の振る舞い")
            conn.commit()

            row = conn.execute(
                "SELECT trigger_mode FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()

            assert row["trigger_mode"] == "intelligently"
        finally:
            conn.close()

    def test_promotion_rejects_content_exactly_100_chars(self, temp_db):
        """contentがちょうど100字だと昇格を拒否する（境界値）"""
        habit_id = add_habit("あ" * 100)["habit_id"]

        result = update_habit(habit_id, trigger_mode="always")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_promotion_allows_content_99_chars(self, temp_db):
        """contentが99字だと昇格を許可する（境界値）"""
        habit_id = add_habit("あ" * 99)["habit_id"]

        result = update_habit(habit_id, trigger_mode="always")

        assert "error" not in result
        assert result["trigger_mode"] == "always"

    def test_promotion_rejected_when_pool_over_capacity_and_total_increases(self, temp_db):
        """プールが定員超過状態のとき、合計を増やす昇格は拒否される"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
            _make_always(conn, "あ" * (ALWAYS_POOL_CAPACITY + 100))
        finally:
            conn.close()

        habit_id = add_habit("あ" * 50)["habit_id"]

        result = update_habit(habit_id, trigger_mode="always")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_demote_then_promote_swap_reduces_total(self, temp_db):
        """プール超過状態でも、降格→昇格の順で合計が減る操作列は通る"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
            big_id = _make_always(conn, "あ" * (ALWAYS_POOL_CAPACITY + 100))
        finally:
            conn.close()

        small_id = add_habit("あ" * 50)["habit_id"]

        demote_result = update_habit(big_id, trigger_mode="intelligently")
        assert "error" not in demote_result

        promote_result = update_habit(small_id, trigger_mode="always")

        assert "error" not in promote_result
        assert promote_result["trigger_mode"] == "always"

    def test_promotion_allowed_within_capacity(self, temp_db):
        """プールが定員以下のとき、定員内に収まる昇格は通る"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
            _make_always(conn, "あ" * (ALWAYS_POOL_CAPACITY - 100))
        finally:
            conn.close()

        habit_id = add_habit("あ" * 50)["habit_id"]

        result = update_habit(habit_id, trigger_mode="always")

        assert "error" not in result
        assert result["trigger_mode"] == "always"

    def test_promotion_rejected_when_exceeding_capacity(self, temp_db):
        """プールが定員以下のとき、定員を超える昇格は拒否される"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
            _make_always(conn, "あ" * (ALWAYS_POOL_CAPACITY - 30))
        finally:
            conn.close()

        habit_id = add_habit("あ" * 50)["habit_id"]

        result = update_habit(habit_id, trigger_mode="always")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_demotion_allowed_even_when_pool_over_capacity(self, temp_db):
        """降格はプール超過状態でも無条件で通る"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
            big_id = _make_always(conn, "あ" * (ALWAYS_POOL_CAPACITY + 100))
        finally:
            conn.close()

        result = update_habit(big_id, trigger_mode="intelligently")

        assert "error" not in result
        assert result["trigger_mode"] == "intelligently"

    def test_deactivation_allowed_even_when_pool_over_capacity(self, temp_db):
        """無効化はプール超過状態でも無条件で通る"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
            big_id = _make_always(conn, "あ" * (ALWAYS_POOL_CAPACITY + 100))
        finally:
            conn.close()

        result = update_habit(big_id, active=False)

        assert "error" not in result
        assert result["active"] == 0

    def test_promotion_and_deactivate_in_one_call_rejects_long_content(self, temp_db):
        """trigger_mode='always'とactive=Falseを同時指定しても、
        100字以上のcontentは拒否される（レビュー指摘の手順Aの1呼び出し目）"""
        habit_id = add_habit("あ" * 50)["habit_id"]

        result = update_habit(
            habit_id, trigger_mode="always", active=False, content="あ" * 300
        )

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

        # 昇格ゲートで拒否されているため、DB上もintelligentyのままで
        # 100字以上のcontentが保存されていないこと
        row = get_habits(habit_id=habit_id)["habits"][0]
        assert row["trigger_mode"] == "intelligently"
        assert row["content"] == "あ" * 50

    def test_promotion_then_deactivate_then_reactivate_rejects_long_content(self, temp_db):
        """trigger_mode='always'とactive=Falseを同時指定する手順が拒否された後、
        短いcontentで正規に無効化→再有効化しても100字制限は健全に機能する
        （レビュー指摘の手順A: 複数手順による回避ができないこと）"""
        habit_id = add_habit("あ" * 90)["habit_id"]

        promote_result = update_habit(habit_id, trigger_mode="always")
        assert "error" not in promote_result

        deactivate_result = update_habit(habit_id, active=False)
        assert "error" not in deactivate_result

        # 無効化中にtrigger_modeを再指定しつつcontentを100字以上に伸ばそうとすると拒否される
        bypass_result = update_habit(
            habit_id, trigger_mode="always", content="あ" * 300
        )
        assert "error" in bypass_result
        assert bypass_result["error"]["code"] == "VALIDATION_ERROR"

        reactivate_result = update_habit(habit_id, active=True)
        assert "error" not in reactivate_result

        row = get_habits(habit_id=habit_id)["habits"][0]
        assert row["content"] == "あ" * 90

    def test_content_update_on_active_always_habit_rejects_long_content(self, temp_db):
        """既にtrigger_mode='always'かつactiveなhabitへのcontent更新も、
        trigger_modeを指定しない場合でも100字制限が課される
        （レビュー指摘の手順B: 昇格後にcontentを伸ばす回避）"""
        habit_id = add_habit("あ" * 90)["habit_id"]

        promote_result = update_habit(habit_id, trigger_mode="always")
        assert "error" not in promote_result

        result = update_habit(habit_id, content="あ" * 300)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

        row = get_habits(habit_id=habit_id)["habits"][0]
        assert row["content"] == "あ" * 90
        assert row["trigger_mode"] == "always"

    def test_content_update_on_active_always_habit_allows_short_content(self, temp_db):
        """既にalwaysかつactiveなhabitでも、100字未満のcontent更新は許可される
        （正常系が壊れていないことの確認）"""
        habit_id = add_habit("あ" * 90)["habit_id"]

        promote_result = update_habit(habit_id, trigger_mode="always")
        assert "error" not in promote_result

        result = update_habit(habit_id, content="あ" * 95)

        assert "error" not in result
        assert result["content"] == "あ" * 95
