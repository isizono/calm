"""update_activityのユニットテスト"""
import os
import tempfile
import pytest
from src.db import get_connection, init_database
from src.services import goal_service as gs
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


def _activity_row(activity_id: int):
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    finally:
        conn.close()


def _new_goal(activity_id: int, handle: str, conditions=None, statement: str = "終わりの一文"):
    conditions = conditions or [{"statement": "条件1", "actor": "claude"}]
    return gs.set_goal(
        activity_id,
        {"new": {"handle": handle, "statement": statement, "conditions": conditions}},
    )


# ========================================
# closed_by / closed_reason / goal_hint
# ========================================


class TestUpdateActivityClosedByValidation:
    def test_closed_by_without_completed_status_rejected(self, test_activity):
        """status='completed'以外でclosed_byを渡すとVALIDATION_ERRORになる"""
        result = update_activity(test_activity["activity_id"], status="in_progress", closed_by="user")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_closed_by_without_any_status_rejected(self, test_activity):
        """statusを渡さずclosed_byだけ渡してもVALIDATION_ERRORになる。
        closed_byは渡されているため「何も渡していない」旨のメッセージにはならない"""
        result = update_activity(test_activity["activity_id"], closed_by="user")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "completed" in result["error"]["message"]
        assert "At least one of" not in result["error"]["message"]

    def test_invalid_status_with_closed_by_reports_invalid_status(self, test_activity):
        """無効なstatusとclosed_byを同時に渡した場合、statusの妥当性チェックが
        先に行われINVALID_STATUSになる（closed_by由来の無関係な理由にはならない）"""
        result = update_activity(test_activity["activity_id"], status="bogus", closed_by="user")

        assert "error" in result
        assert result["error"]["code"] == "INVALID_STATUS"

    def test_closed_reason_without_completed_status_rejected(self, test_activity):
        result = update_activity(test_activity["activity_id"], title="x", closed_reason="理由")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_closed_by_value_rejected(self, test_activity):
        """'goal_judge'はサーバー専用の値であり、引数としては受け付けない"""
        result = update_activity(test_activity["activity_id"], status="completed", closed_by="goal_judge")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestUpdateActivityClosedByWrite:
    def test_explicit_closed_by_and_reason_written(self, test_activity):
        activity_id = test_activity["activity_id"]

        result = update_activity(activity_id, status="completed", closed_by="user", closed_reason="ユーザー宣言")

        assert "error" not in result
        row = _activity_row(activity_id)
        assert row["closed_by"] == "user"
        assert row["closed_reason"] == "ユーザー宣言"
        assert row["closed_at"] is not None

    def test_no_closed_by_and_no_linked_goal_writes_null(self, test_activity):
        """closed_by省略・紐づくgoal無しならclosed_byはNULL（不明）になる"""
        activity_id = test_activity["activity_id"]

        result = update_activity(activity_id, status="completed", closed_reason="理由だけ")

        assert "error" not in result
        row = _activity_row(activity_id)
        assert row["closed_by"] is None
        assert row["closed_reason"] == "理由だけ"

    def test_no_closed_by_with_unjudged_linked_goal_writes_null(self, test_activity):
        """closed_by省略・紐づくgoalはあるが未判定ならclosed_byはNULL（不明）になる。
        goal_closedの判定を経ずに「紐づくgoalがあるから」で'goal_judge'を書いてしまう
        リグレッションを検知する"""
        activity_id = test_activity["activity_id"]
        _new_goal(activity_id, "update-activity-unjudged")

        result = update_activity(activity_id, status="completed", closed_reason="未判定のまま閉じる")

        assert "error" not in result
        row = _activity_row(activity_id)
        assert row["closed_by"] is None
        assert row["closed_reason"] == "未判定のまま閉じる"

    def test_no_closed_by_with_judged_goal_writes_goal_judge(self, test_activity):
        """closed_by省略・紐づくgoalが判定済みなら'goal_judge'が書かれ、
        closed_reason省略時はgoals.judge_noteが使われる"""
        activity_id = test_activity["activity_id"]
        created = _new_goal(
            activity_id,
            "update-activity-g1",
            conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}],
        )
        goal_id = created["goal"]["goal_id_raw"]
        gs.judge_goal(goal_id, "achieved", note="達成した")
        # judge_goalが既にcompletedにしているので、読むために開いてから閉じ直す
        update_activity(activity_id, status="in_progress")
        # judge_goal由来のclosed_by残留で偽陽性にならないよう、いったん別の値に
        # 書き換えてから閉じ直す（この後の書き込みがresolved_closed_byの
        # 決定ロジック自体で"goal_judge"を書いていることを確かめるため）
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE activities SET closed_by = 'external', closed_reason = '古い理由' WHERE id = ?",
                (activity_id,),
            )
            conn.commit()
        finally:
            conn.close()

        result = update_activity(activity_id, status="completed")

        assert "error" not in result
        row = _activity_row(activity_id)
        assert row["closed_by"] == "goal_judge"
        assert row["closed_reason"] == "達成した"

    def test_no_closed_by_with_judged_goal_and_explicit_reason_overrides_judge_note(self, test_activity):
        activity_id = test_activity["activity_id"]
        created = _new_goal(
            activity_id,
            "update-activity-g2",
            conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}],
        )
        goal_id = created["goal"]["goal_id_raw"]
        gs.judge_goal(goal_id, "achieved", note="達成した")
        update_activity(activity_id, status="in_progress")

        result = update_activity(activity_id, status="completed", closed_reason="別の理由で閉じ直す")

        assert "error" not in result
        row = _activity_row(activity_id)
        assert row["closed_by"] == "goal_judge"
        assert row["closed_reason"] == "別の理由で閉じ直す"

    def test_already_completed_activity_does_not_rewrite_closed_fields(self, test_activity):
        """既にcompletedのactivityにstatus='completed'を渡してもclosed_*は書き換えない"""
        activity_id = test_activity["activity_id"]
        update_activity(activity_id, status="completed", closed_by="user", closed_reason="1回目")

        result = update_activity(activity_id, status="completed", closed_by="external", closed_reason="2回目")

        assert "error" not in result
        row = _activity_row(activity_id)
        assert row["closed_by"] == "user"
        assert row["closed_reason"] == "1回目"


class TestUpdateActivityGoalHint:
    def test_goal_hint_absent_without_linked_goal(self, test_activity):
        result = update_activity(test_activity["activity_id"], status="completed", closed_by="user")

        assert "error" not in result
        assert "goal_hint" not in result

    def test_goal_hint_present_and_warns_when_last_activity_closed(self, test_activity):
        """未判定goalの最後のactivityを閉じると、goal_hintにwarningが載り、
        nextは閉じたactivityを指定した読み出しとして評価されたまま（06 §3.6）"""
        activity_id = test_activity["activity_id"]
        _new_goal(activity_id, "update-activity-hint-1")

        result = update_activity(activity_id, status="completed", closed_by="user")

        assert "error" not in result
        assert result["goal_hint"]["handle"] == "update-activity-hint-1"
        assert "warning" in result["goal_hint"]
        # goalはまだactive（条件1件が未終端）なので、nextは規則11（Claudeの手番）が
        # そのままその条件を指す
        assert result["goal_hint"]["next"]["rule"] == 11

    def test_goal_hint_includes_open_questions_when_judge_ready(self, test_activity):
        """判定待ちのgoalで、まだ未完了の別activityが未決askにブロックされていれば、
        goal_hintにopen_questionsが載る（06 §3.6・05 第1節の入力6）"""
        from src.services import ask_service as ak

        activity_id = test_activity["activity_id"]
        created = _new_goal(
            activity_id,
            "update-activity-hint-oq",
            conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}],
        )
        goal_id = created["goal"]["goal_id_raw"]
        sibling_id = add_activity(
            title="兄弟activity", description="desc", tags=["domain:test"], check_in=False
        )["activity_id"]
        gs.set_goal(sibling_id, {"goal_id": goal_id})
        ak.add_ask("残ってる問い", tags=["domain:test"], blocks=[sibling_id], notify=False)

        result = update_activity(activity_id, status="completed", closed_by="user")

        assert "error" not in result
        assert result["goal_hint"]["label"] == "judge_ready"
        assert result["goal_hint"]["open_activities_left"] == 1
        assert "warning" not in result["goal_hint"]
        titles = {q["title"] for q in result["goal_hint"]["open_questions"]}
        assert "残ってる問い" in titles

    def test_goal_hint_absent_when_goal_already_judged(self, test_activity):
        """紐づくgoalが既に判定済みならgoal_hintは付かない"""
        activity_id = test_activity["activity_id"]
        created = _new_goal(
            activity_id,
            "update-activity-hint-2",
            conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}],
        )
        goal_id = created["goal"]["goal_id_raw"]
        gs.judge_goal(goal_id, "achieved", note="達成")
        update_activity(activity_id, status="in_progress")

        result = update_activity(activity_id, status="completed")

        assert "error" not in result
        assert "goal_hint" not in result

    def test_goal_hint_build_failure_does_not_lose_completion(self, test_activity, monkeypatch):
        """goal_hintの組み立てで例外が出ても、completedへの更新自体は保持される"""
        activity_id = test_activity["activity_id"]
        _new_goal(activity_id, "update-activity-hint-3")

        monkeypatch.setattr(
            gs, "build_goal_hint",
            lambda conn, aid: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        result = update_activity(activity_id, status="completed", closed_by="user")

        assert "error" not in result
        assert result["goal_hint"]["error"]["code"] == "DATABASE_ERROR"
        row = _activity_row(activity_id)
        assert row["status"] == "completed"
        assert row["closed_by"] == "user"
