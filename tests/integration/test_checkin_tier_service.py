"""checkin_tier_service.collect_and_assembleの統合テスト
（pinned集合・asks・goal・dependencies・statusの遷移・別名の記録・
goal失敗の隔離）。呼び出しは各テストで別々のactivity・session_idを使い、
セッション別の既出管理（tag_notes注入済み記録・flow_guide初回判定）が
テスト間で干渉しないようにする。
"""
import pytest

import src.services.checkin_tier_service as checkin_tier_service
from src.db import get_connection
from src.infra import session_identity
from src.services import goal_service as gs
from src.services import session_registry_service
from src.services.activity_service import add_activity, update_activity
from src.services.ask_service import add_ask_with_conn
from src.services.material_service import add_material
from src.services.pin_service import add_pin
from src.services.topic_service import add_topic
from tests.helpers import add_decision, add_log

DEFAULT_TAGS = ["domain:test"]


def _make_activity(title: str, *, status: str | None = None) -> int:
    result = add_activity(title=title, description=f"{title}の説明", tags=DEFAULT_TAGS, check_in=False)
    activity_id = result["activity_id"]
    if status is not None:
        update_activity(activity_id, status=status)
    return activity_id


def _add_ask(conn, activity_id: int, question: str, *, answered: bool = False, answer_body: str = "") -> int:
    result = add_ask_with_conn(conn, question, [activity_id], DEFAULT_TAGS)
    assert "error" not in result
    ask_id = result["id"]
    if answered:
        conn.execute(
            "UPDATE asks SET status = 'answered', answer_body = ?, answered_at = CURRENT_TIMESTAMP WHERE id = ?",
            (answer_body, ask_id),
        )
    conn.commit()
    return ask_id


class TestPinned:
    def test_pinned_set_collected_correctly(self, temp_db):
        topic = add_topic(title="pinned検証用トピック", description="pinned検証用", tags=DEFAULT_TAGS)
        decision = add_decision("決定X", "理由X", topic["topic_id"])
        log = add_log(topic["topic_id"], title="ログX", content="本文X")
        material = add_material(title="資材X", content="内容X", source="test", tags=DEFAULT_TAGS)

        activity_id = _make_activity("pinned検証")
        add_pin("activity", activity_id, "decision", decision["decision_id"])
        add_pin("activity", activity_id, "log", log["log_id"])
        add_pin("activity", activity_id, "material", material["material_id"])

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="pinned-check")

        assert "error" not in result
        pinned = result["anchor"]["pinned"]
        assert set(pinned.keys()) == {"decisions", "logs", "materials"}
        assert pinned["decisions"][0]["id_raw"] == decision["decision_id"]
        assert pinned["logs"][0]["id_raw"] == log["log_id"]
        assert pinned["materials"][0]["id_raw"] == material["material_id"]


class TestAsks:
    def test_asks_content_under_cap(self, temp_db):
        activity_id = _make_activity("asks検証")
        conn = get_connection()
        try:
            _add_ask(conn, activity_id, "未回答の質問")
            _add_ask(conn, activity_id, "回答済み未トリアージの質問", answered=True, answer_body="回答本文")
        finally:
            conn.close()

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="asks-check")

        asks = result["control"]["asks"]
        assert len(asks["awaiting_answer"]) == 1
        assert len(asks["awaiting_triage"]) == 1
        assert asks["awaiting_triage"][0]["answer_body"] == "回答本文"
        assert "more" not in asks

    def test_asks_over_cap_folds_to_more_with_pointer_and_truncates_answer_body(self, temp_db):
        activity_id = _make_activity("asks上限超過検証")
        conn = get_connection()
        try:
            for i in range(4):
                _add_ask(conn, activity_id, f"未回答の質問{i}")
            long_body = "あ" * 500
            for i in range(3):
                _add_ask(conn, activity_id, f"回答済みの質問{i}", answered=True, answer_body=long_body)
        finally:
            conn.close()

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="asks-overflow")

        asks = result["control"]["asks"]
        kept_total = len(asks["awaiting_answer"]) + len(asks["awaiting_triage"])
        assert kept_total == checkin_tier_service.ASKS_MAX
        assert asks["more"] == 7 - checkin_tier_service.ASKS_MAX
        assert asks["next"] == [{"tool": "get_asks", "args": {"blocking_activity_id": activity_id}}]
        for item in asks["awaiting_triage"]:
            assert len(item["answer_body"]) == checkin_tier_service.ASK_ANSWER_BODY_MAX_CHARS
            assert item["answer_truncated"] is True


class TestDependencies:
    def test_dependencies_content_under_cap(self, temp_db):
        dep = _make_activity("依存先")
        main_id = _make_activity("依存関係検証")
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (main_id, dep),
            )
            conn.commit()
        finally:
            conn.close()

        result = checkin_tier_service.collect_and_assemble(main_id, session_id="deps-check")

        deps = result["control"]["dependencies"]
        assert len(deps) == 1
        assert deps[0]["id_raw"] == dep

    def test_dependencies_over_cap_folds_to_items_and_more_with_pointer(self, temp_db):
        main_id = _make_activity("依存関係上限超過検証")
        conn = get_connection()
        try:
            dep_ids = [_make_activity(f"依存先{i}") for i in range(12)]
            for dep_id in dep_ids:
                conn.execute(
                    "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                    (main_id, dep_id),
                )
            conn.commit()
        finally:
            conn.close()

        result = checkin_tier_service.collect_and_assemble(main_id, session_id="deps-overflow")

        deps = result["control"]["dependencies"]
        assert len(deps["items"]) == checkin_tier_service.DEPENDENCIES_MAX
        assert deps["more"] == 12 - checkin_tier_service.DEPENDENCIES_MAX
        assert deps["next"] == [{"tool": "get_map", "args": {"entity_type": "activity", "entity_id": main_id}}]


class TestGoal:
    def test_goal_block_active_label(self, temp_db):
        activity_id = _make_activity("goal検証")
        gs.set_goal(
            activity_id,
            {"new": {"handle": "tier-goal-check", "statement": "終わりの一文", "conditions": [
                {"statement": "条件1", "actor": "claude"}
            ]}},
        )

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="goal-check")

        goal = result["control"]["goal"]
        assert goal["label"] == "active"
        assert goal["handle"] == "tier-goal-check"
        assert goal["statement"] == "終わりの一文"

    def test_goal_failure_is_isolated(self, temp_db, monkeypatch):
        """goalブロック組み立てで例外が出ても、他のキーは失われずgoalにerrorの形が
        載る。machine_errorのsignalがcheck_inの接続で記録される。
        """
        activity_id = _make_activity("goal失敗検証")
        monkeypatch.setattr(
            gs, "build_goal_block_for_activity", lambda conn, aid: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="goal-fail-check")

        expected_error_goal = {"error": {"code": "DATABASE_ERROR", "message": "goal ブロックを組み立てられなかった"}}
        assert result["control"]["goal"] == expected_error_goal
        assert result["anchor"]["activity"]["status"] == "in_progress"

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT detail FROM signal_events WHERE kind = 'machine_error' AND source = 'tool:check_in'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert "boom" in rows[0]["detail"]


class TestStatusTransition:
    """completedのactivityへのcheck_inが、asks収集より先にin_progressへ遷移することを
    固定する（既決: 遷移が先でないと、再オープンしたactivityのブロックaskが
    completed扱いで除外されたままになる）。
    """

    def test_reopens_completed_activity_and_delivers_blocking_ask(self, temp_db):
        activity_id = _make_activity("完了済み再開検証")
        conn = get_connection()
        try:
            _add_ask(conn, activity_id, "完了済みactivityをブロックする質問")
        finally:
            conn.close()
        update_activity(activity_id, status="completed")

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="reopen-check")

        assert result["anchor"]["activity"]["status"] == "in_progress"
        assert "asks" in result["control"]
        assert len(result["control"]["asks"]["awaiting_answer"]) == 1


class TestSessionAlias:
    """セッション別名レジストリへの登録を検証する。"""

    @pytest.fixture(autouse=True)
    def _isolate_registry_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv(session_registry_service.REGISTRY_PATH_ENV, str(tmp_path / "session_aliases.json"))

    def _stub_world(self, monkeypatch, bridge_session_id: str, cli_session_id: str, cli_pid: int, name: str):
        def resolve(bsid):
            if bsid != bridge_session_id:
                return None
            return {"cli_pid": cli_pid, "cli_session_id": cli_session_id, "name": name, "cwd": None, "cli_status": None}

        monkeypatch.setattr(session_identity, "resolve_cli_session", resolve)
        monkeypatch.setattr(session_registry_service, "is_process_alive", lambda pid: pid == cli_pid)
        monkeypatch.setattr(
            session_registry_service.cli_session,
            "read_cli_session",
            lambda pid: resolve(bridge_session_id) if pid == cli_pid else None,
        )
        monkeypatch.setattr(session_identity, "get_caller_session_id", lambda: bridge_session_id)

    def test_session_field_populated_when_cli_resolved(self, temp_db, monkeypatch):
        activity_id = _make_activity("セッション別名検証")
        self._stub_world(monkeypatch, "bridge-new", "cli-new", 200, "workspace-new")

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="bridge-new")

        assert result["env"]["session"] == {
            "name": "workspace-new",
            "alias": "セッション別名検証",
            "alias_collision": False,
        }

    def test_session_field_reports_unresolved_when_bridge_id_missing(self, temp_db, monkeypatch):
        monkeypatch.setattr(session_identity, "get_caller_session_id", lambda: None)

        activity_id = _make_activity("セッション別名未解決検証")
        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="unresolved-check")

        assert result["env"]["session"] == {"registered": False, "reason": "cli_unresolved"}
