"""hint_service: 統一hint APIのユニットテスト"""
import os
import tempfile
from datetime import date

import pytest

import src.services.hint_service as hint_service
from src.db import get_connection, init_database
from src.services.activity_service import add_activity
from src.services.decision_service import add_decisions
from src.services.direction_service import DIRECTION_NAME, DIRECTION_NAMESPACE
from src.services.hint_service import (
    ACTIVITY_CLEANUP_AUTOTRIGGER_GUARD,
    ACTIVITY_CLEANUP_COUNT_THRESHOLD,
    DIRECTION_OVERFLOW_THRESHOLD,
    HINT_LOGS_SPARSE_MESSAGE,
    LOGS_SPARSE_LOG_THRESHOLD,
    MARKER_ACTIVITY_CLEANUP,
    MARKER_DIRECTION_OVERFLOW,
    MARKER_LOGS_SPARSE,
    MARKER_RECOMPOSE_BOOTSTRAP,
    MARKER_RECOMPOSE_DELTA,
    MARKER_RECOMPOSE_GENERIC,
    RECOMPOSE_AUTOTRIGGER_GUARD,
    RECOMPOSE_BOOTSTRAP_THRESHOLD,
    RECOMPOSE_DELTA_THRESHOLD,
    _count_stale_activities,
    _is_marker_active,
    _merge_cooldown_marker,
    get_hints,
    get_hints_with_conn,
    is_orch_managed_activity,
)
from src.services.material_service import add_material
from src.services.pin_service import add_pin
from src.services.topic_service import add_topic
from src.services.tag_service import _injected_tags, update_tag
from tests.helpers import add_decision

DOMAIN_TAG_NAME = "hint-domain"
DOMAIN_TAG = f"domain:{DOMAIN_TAG_NAME}"
DIRECTION_TAG = f"{DIRECTION_NAMESPACE}:{DIRECTION_NAME}"
ACTIVITY_MANAGEMENT_TAG_NAME = "activity-management"
# activity-managementは素タグ(namespace無し)として運用する。
# domain:を付けない点が本テストの前提であり、そこを間違えると本番のタグ形状
# (namespace='')と乖離した状態でテストが通ってしまう。
ACTIVITY_MANAGEMENT_TAG = ACTIVITY_MANAGEMENT_TAG_NAME
STALE_TARGET_TAG = "domain:activity-cleanup-target"
STALE_TS = "2000-01-01 00:00:00"


def _add_direction_decision(topic_id: int, i: int) -> dict:
    result = add_decisions([{
        "topic_id": topic_id, "decision": f"方向性{i}", "reason": "r", "title": f"方向性{i}の要点",
        "tags": [DIRECTION_TAG],
    }])
    assert "error" not in result, result
    return result["created"][0]


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _tag_id(name: str, namespace: str = "domain") -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM tags WHERE namespace = ? AND name = ?",
            (namespace, name),
        ).fetchone()
        return row["id"]
    finally:
        conn.close()


def _get_tag_notes(name: str, namespace: str = "domain") -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT notes FROM tags WHERE namespace = ? AND name = ?",
            (namespace, name),
        ).fetchone()
        return row["notes"] or ""
    finally:
        conn.close()


def _fixed_date(y: int, m: int, d: int) -> type:
    """date.today()が固定値を返すdateサブクラスを生成する(monkeypatch用)。"""
    fixed = date(y, m, d)

    class _FixedDate(date):
        @classmethod
        def today(cls):
            return fixed

    return _FixedDate


def _set_material_updated_at(material_id: int, ts: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE materials SET updated_at = ? WHERE id = ?",
            (ts, material_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_decision_created_at(decision_id: int, ts: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE decisions SET created_at = ? WHERE id = ?",
            (ts, decision_id),
        )
        conn.commit()
    finally:
        conn.close()


def _ensure_activity_management_tag() -> None:
    """activity-managementタグをtags行として存在させる(notes未設定の状態)。

    activityやdecisionを紐付ける必要はないため、add_topic経由でタグだけ作る。
    """
    add_topic(title="am-anchor", description="d", tags=[ACTIVITY_MANAGEMENT_TAG])


def _make_activity_for_cleanup(
    status: str = "pending",
    orch_managed: bool = False,
    updated_at: str | None = STALE_TS,
    heartbeat_at: str | None = None,
    tag: str = STALE_TARGET_TAG,
) -> int:
    """放置件数判定の母集団に入るactivityを作る。既定でstatus=pending・
    updated_at=STALE_TS(放置扱い)・last_heartbeat_at未設定(NULL)。

    tag引数はactivity_cleanupがdomain:tagの有無に依存しないことを検証する
    テスト用に、素タグ(namespace無し)を指定できるようにするためのもの。
    放置件数の母集団判定(_count_stale_activities)自体はtagを一切参照しない。"""
    activity = add_activity(
        title="[作業] x", description="d",
        tags=[tag],
        check_in=False,
        orch_managed=orch_managed,
    )
    activity_id = activity["activity_id"]
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE activities SET status = ? WHERE id = ?", (status, activity_id)
        )
        if updated_at is not None:
            conn.execute(
                "UPDATE activities SET updated_at = ? WHERE id = ?",
                (updated_at, activity_id),
            )
        if heartbeat_at is not None:
            conn.execute(
                "UPDATE activities SET last_heartbeat_at = ? WHERE id = ?",
                (heartbeat_at, activity_id),
            )
        conn.commit()
    finally:
        conn.close()
    return activity_id


class TestRecomposeBootstrap:
    def test_fires_at_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert len(hints) == 1
        assert hints[0]["type"] == "recompose_bootstrap"
        assert hints[0]["delivery_hint"] == "immediate"
        assert hints[0]["severity"] == "info"
        assert str(RECOMPOSE_BOOTSTRAP_THRESHOLD) in hints[0]["message"]

    def test_message_includes_autotrigger_guard(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert RECOMPOSE_AUTOTRIGGER_GUARD in hints[0]["message"]

    def test_silent_below_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD - 1):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []

    def test_plain_tag_namespace_not_targeted(self, temp_db):
        """素タグ namespace='' は判定対象外。namespaceフィルタが効いていることを確認。"""
        plain_topic = add_topic(
            title="t", description="d", tags=["plain-only"]
        )
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD + 5):
            add_decision(decision=f"d{i}", reason="r", topic_id=plain_topic["topic_id"])

        plain_tag_id = _tag_id("plain-only", namespace="")
        assert get_hints("tag", plain_tag_id) == []

    def test_suppressed_by_specific_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
        update_tag(DOMAIN_TAG, notes=f"既に整理済。{MARKER_RECOMPOSE_BOOTSTRAP}")

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []

    def test_suppressed_by_generic_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
        update_tag(DOMAIN_TAG, notes=f"{MARKER_RECOMPOSE_GENERIC} 任意ノート")

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []


class TestRecomposeDelta:
    def test_fires_at_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        mat = add_material(
            title="m", content="c", tags=[DOMAIN_TAG], source="s",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        for i in range(RECOMPOSE_DELTA_THRESHOLD):
            d = add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
            _set_decision_created_at(d["decision_id"], "2024-07-01 00:00:00")

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert len(hints) == 1
        assert hints[0]["type"] == "recompose_delta"
        assert hints[0]["delivery_hint"] == "immediate"
        assert str(RECOMPOSE_DELTA_THRESHOLD) in hints[0]["message"]

    def test_message_includes_autotrigger_guard(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        mat = add_material(
            title="m", content="c", tags=[DOMAIN_TAG], source="s",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        for i in range(RECOMPOSE_DELTA_THRESHOLD):
            d = add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
            _set_decision_created_at(d["decision_id"], "2024-07-01 00:00:00")

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert RECOMPOSE_AUTOTRIGGER_GUARD in hints[0]["message"]

    def test_silent_below_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        mat = add_material(
            title="m", content="c", tags=[DOMAIN_TAG], source="s",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        for i in range(RECOMPOSE_DELTA_THRESHOLD - 1):
            d = add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
            _set_decision_created_at(d["decision_id"], "2024-07-01 00:00:00")

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []

    def test_decisions_before_base_time_excluded(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        mat = add_material(
            title="m", content="c", tags=[DOMAIN_TAG], source="s",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        for i in range(RECOMPOSE_DELTA_THRESHOLD):
            d = add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
            _set_decision_created_at(d["decision_id"], "2024-05-01 00:00:00")

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []

    def test_suppressed_by_delta_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        mat = add_material(
            title="m", content="c", tags=[DOMAIN_TAG], source="s",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        for i in range(RECOMPOSE_DELTA_THRESHOLD):
            d = add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
            _set_decision_created_at(d["decision_id"], "2024-07-01 00:00:00")
        update_tag(DOMAIN_TAG, notes=f"{MARKER_RECOMPOSE_DELTA}")

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []


class TestDirectionOverflow:
    def test_fires_at_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(DIRECTION_OVERFLOW_THRESHOLD):
            _add_direction_decision(topic["topic_id"], i)

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        direction_hints = [h for h in hints if h["type"] == "direction_overflow"]
        assert len(direction_hints) == 1
        assert direction_hints[0]["delivery_hint"] == "immediate"
        assert direction_hints[0]["severity"] == "info"
        assert str(DIRECTION_OVERFLOW_THRESHOLD) in direction_hints[0]["message"]

    def test_silent_below_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(DIRECTION_OVERFLOW_THRESHOLD - 1):
            _add_direction_decision(topic["topic_id"], i)

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert [h for h in hints if h["type"] == "direction_overflow"] == []

    def test_suppressed_by_direction_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(DIRECTION_OVERFLOW_THRESHOLD):
            _add_direction_decision(topic["topic_id"], i)
        update_tag(DOMAIN_TAG, notes=f"{MARKER_DIRECTION_OVERFLOW}")

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert [h for h in hints if h["type"] == "direction_overflow"] == []

    def test_not_suppressed_by_generic_recompose_marker(self, temp_db):
        """direction_overflowはrecompose系と独立した抑制マーカーを持つ。
        汎用recomposeマーカーでは抑制されない"""
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(DIRECTION_OVERFLOW_THRESHOLD):
            _add_direction_decision(topic["topic_id"], i)
        update_tag(DOMAIN_TAG, notes=f"{MARKER_RECOMPOSE_GENERIC}")

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert [h for h in hints if h["type"] == "direction_overflow"] != []

    def test_excludes_retracted_and_superseded_from_count(self, temp_db):
        """有効(active)件数のみをカウントする。件数不足ならfireしない"""
        from src.services.relation_service import add_relation
        from tests.helpers import retract_decision

        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        decisions = [_add_direction_decision(topic["topic_id"], i) for i in range(DIRECTION_OVERFLOW_THRESHOLD)]
        retract_decision(decisions[0]["decision_id"])
        add_relation(
            "decision", decisions[1]["decision_id"],
            [{"type": "decision", "ids": [decisions[2]["decision_id"]]}],
            relation_type="supersedes",
        )

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert [h for h in hints if h["type"] == "direction_overflow"] == []

    def test_recompose_marker_does_not_suppress_when_scoped_to_delta(self, temp_db):
        """coexistence: recompose_bootstrapとdirection_overflowが同時に発火しうる"""
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
        for i in range(DIRECTION_OVERFLOW_THRESHOLD):
            _add_direction_decision(topic["topic_id"], i)

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        types = {h["type"] for h in hints}
        assert "recompose_bootstrap" in types
        assert "direction_overflow" in types


class TestLogsSparse:
    def test_fires_when_logs_below_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        add_decision(decision="d", reason="r", topic_id=topic["topic_id"])

        hints = get_hints("topic", topic["topic_id"])
        assert len(hints) == 1
        assert hints[0]["type"] == "logs_sparse"
        assert hints[0]["delivery_hint"] == "deferred"
        assert hints[0]["severity"] == "info"
        assert hints[0]["message"] == HINT_LOGS_SPARSE_MESSAGE

    def test_silent_when_no_decisions(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        assert get_hints("topic", topic["topic_id"]) == []

    def test_silent_when_logs_at_threshold(self, temp_db):
        from tests.helpers import add_log

        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        for i in range(LOGS_SPARSE_LOG_THRESHOLD):
            add_log(topic_id=topic["topic_id"], content=f"l{i}")

        assert get_hints("topic", topic["topic_id"]) == []

    def test_suppressed_by_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        update_tag(DOMAIN_TAG, notes=f"以後logsは付けない方針。{MARKER_LOGS_SPARSE}")

        assert get_hints("topic", topic["topic_id"]) == []


class TestActivityScope:
    def test_aggregates_domain_tag_recompose_hints(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
        dec0 = add_decision(decision="anchor", reason="r", topic_id=topic["topic_id"])
        activity = add_activity(
            title="[作業] x", description="d",
            tags=[DOMAIN_TAG, "intent:implement"],
            related=[{"type": "decision", "ids": [dec0["decision_id"]]}],
            check_in=False,
        )

        hints = get_hints("activity", activity["activity_id"])
        assert any(h["type"] == "recompose_bootstrap" for h in hints)


class TestIsOrchManagedActivity:
    def test_true_when_orch_managed_column_set(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        dec = add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        a = add_activity(
            title="[orch] x", description="d",
            tags=[DOMAIN_TAG, "intent:implement"],
            related=[{"type": "decision", "ids": [dec["decision_id"]]}],
            check_in=False,
            orch_managed=True,
        )
        conn = get_connection()
        try:
            assert is_orch_managed_activity(conn, a["activity_id"]) is True
        finally:
            conn.close()

    def test_false_when_orch_managed_column_not_set(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        dec = add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        a = add_activity(
            title="[作業] x", description="d",
            tags=[DOMAIN_TAG, "intent:implement"],
            related=[{"type": "decision", "ids": [dec["decision_id"]]}],
            check_in=False,
        )
        conn = get_connection()
        try:
            assert is_orch_managed_activity(conn, a["activity_id"]) is False
        finally:
            conn.close()

    def test_false_when_only_tag_present_without_column(self, temp_db):
        """orch-managed タグだけ付与しても orch_managed カラムが 0 なら False (カラム判定優先)。

        移行期にタグだけ残った状態でも、判定はカラム値のみに依存する。
        """
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        dec = add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        a = add_activity(
            title="[orch] x", description="d",
            tags=[DOMAIN_TAG, "orch-managed", "intent:implement"],
            related=[{"type": "decision", "ids": [dec["decision_id"]]}],
            check_in=False,
        )
        conn = get_connection()
        try:
            assert is_orch_managed_activity(conn, a["activity_id"]) is False
        finally:
            conn.close()

    def test_false_for_unknown_activity_id(self, temp_db):
        """存在しない activity_id は False (フェイルオープン)。"""
        conn = get_connection()
        try:
            assert is_orch_managed_activity(conn, 999_999) is False
        finally:
            conn.close()


class TestIsMarkerActiveHelper:
    """_is_marker_active: 恒久/期限付きマーカー判定の純粋関数テスト（DB不要）"""

    def test_plain_marker_active(self):
        assert _is_marker_active(f"foo {MARKER_LOGS_SPARSE} bar", MARKER_LOGS_SPARSE) is True

    def test_absent_marker_inactive(self):
        assert _is_marker_active("no markers here", MARKER_LOGS_SPARSE) is False

    def test_future_dated_marker_active(self):
        assert _is_marker_active(f"{MARKER_LOGS_SPARSE}-until:2099-01-01", MARKER_LOGS_SPARSE) is True

    def test_past_dated_marker_inactive(self):
        assert _is_marker_active(f"{MARKER_LOGS_SPARSE}-until:2000-01-01", MARKER_LOGS_SPARSE) is False

    def test_invalid_date_format_inactive(self):
        """不正な日付形式(存在しない13月99日)は無視される(フェイルオープン、抑制しない側に倒す)"""
        assert _is_marker_active(f"{MARKER_LOGS_SPARSE}-until:2026-13-99", MARKER_LOGS_SPARSE) is False

    def test_expired_dated_marker_not_mistaken_for_permanent(self):
        """期限切れの日付付きマーカーだけが存在する場合、素の恒久マーカーとして
        誤検出されない(prefix関係の回帰防止: `#foo-until:...`は`#foo`を部分文字列
        として含む)"""
        assert _is_marker_active(f"{MARKER_LOGS_SPARSE}-until:2000-01-01", MARKER_LOGS_SPARSE) is False

    def test_permanent_marker_wins_when_expired_dated_also_present(self):
        notes = f"{MARKER_LOGS_SPARSE} {MARKER_LOGS_SPARSE}-until:2000-01-01"
        assert _is_marker_active(notes, MARKER_LOGS_SPARSE) is True


class TestDatedMarkerSnooze:
    """スヌーズマーカー(`<marker>-until:YYYY-MM-DD`)による期限付き抑制の結線テスト"""

    def test_future_dated_marker_suppresses_recompose_bootstrap(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
        update_tag(DOMAIN_TAG, notes=f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:2099-01-01")

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []

    def test_past_dated_marker_does_not_suppress_recompose_bootstrap(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
        update_tag(DOMAIN_TAG, notes=f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:2000-01-01")

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert any(h["type"] == "recompose_bootstrap" for h in hints)

    def test_future_dated_marker_suppresses_logs_sparse(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        update_tag(DOMAIN_TAG, notes=f"{MARKER_LOGS_SPARSE}-until:2099-01-01")

        assert get_hints("topic", topic["topic_id"]) == []

    def test_past_dated_marker_does_not_suppress_logs_sparse(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        update_tag(DOMAIN_TAG, notes=f"{MARKER_LOGS_SPARSE}-until:2000-01-01")

        hints = get_hints("topic", topic["topic_id"])
        assert any(h["type"] == "logs_sparse" for h in hints)

    def test_recompose_bootstrap_message_includes_snooze_instructions(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        bootstrap_hint = next(h for h in hints if h["type"] == "recompose_bootstrap")
        assert "-until:" in bootstrap_hint["message"]
        assert MARKER_RECOMPOSE_BOOTSTRAP in bootstrap_hint["message"]

    def test_logs_sparse_message_includes_snooze_instructions(self):
        assert "-until:" in HINT_LOGS_SPARSE_MESSAGE
        assert MARKER_LOGS_SPARSE in HINT_LOGS_SPARSE_MESSAGE


class TestMergeCooldownMarkerHelper:
    """_merge_cooldown_marker: 日次クールダウンマーカー更新の純粋関数テスト（DB不要）"""

    def test_no_existing_marker_appends_today(self):
        today = date(2026, 1, 15)
        result = _merge_cooldown_marker("既存メモ", MARKER_RECOMPOSE_BOOTSTRAP, today)
        assert f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:2026-01-15" in result
        assert "既存メモ" in result

    def test_past_dated_marker_updated_to_today(self):
        today = date(2026, 1, 15)
        notes = f"メモ {MARKER_RECOMPOSE_BOOTSTRAP}-until:2026-01-01"
        result = _merge_cooldown_marker(notes, MARKER_RECOMPOSE_BOOTSTRAP, today)
        assert f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:2026-01-15" in result
        assert "2026-01-01" not in result

    def test_today_dated_marker_updated(self):
        """境界値: 既存マーカーの日付がtodayと同一の場合も"今日以前"として更新処理を通す"""
        today = date(2026, 1, 15)
        notes = f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:2026-01-15"
        result = _merge_cooldown_marker(notes, MARKER_RECOMPOSE_BOOTSTRAP, today)
        assert f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:2026-01-15" in result

    def test_future_dated_marker_not_overwritten(self):
        """ユーザーが意図的に設定した未来日の長期抑制は上書きしない"""
        today = date(2026, 1, 15)
        notes = f"メモ {MARKER_RECOMPOSE_BOOTSTRAP}-until:2099-01-01"
        result = _merge_cooldown_marker(notes, MARKER_RECOMPOSE_BOOTSTRAP, today)
        assert result == notes

    def test_invalid_date_format_replaced(self):
        today = date(2026, 1, 15)
        notes = f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:2026-13-99"
        result = _merge_cooldown_marker(notes, MARKER_RECOMPOSE_BOOTSTRAP, today)
        assert f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:2026-01-15" in result
        assert "2026-13-99" not in result

    def test_empty_notes_produces_marker_only(self):
        today = date(2026, 1, 15)
        result = _merge_cooldown_marker("", MARKER_RECOMPOSE_DELTA, today)
        assert result == f"{MARKER_RECOMPOSE_DELTA}-until:2026-01-15"


class TestRecomposeCooldownMarker:
    """hint発火に伴う日次クールダウンマーカーの自動追記・DB結線テスト"""

    def test_bootstrap_fire_appends_cooldown_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert any(h["type"] == "recompose_bootstrap" for h in hints)

        today = date.today().isoformat()
        assert f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:{today}" in _get_tag_notes(DOMAIN_TAG_NAME)

    def test_delta_fire_appends_cooldown_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        mat = add_material(title="m", content="c", tags=[DOMAIN_TAG], source="s")
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        for i in range(RECOMPOSE_DELTA_THRESHOLD):
            d = add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
            _set_decision_created_at(d["decision_id"], "2024-07-01 00:00:00")

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert any(h["type"] == "recompose_delta" for h in hints)

        today = date.today().isoformat()
        assert f"{MARKER_RECOMPOSE_DELTA}-until:{today}" in _get_tag_notes(DOMAIN_TAG_NAME)

    def test_same_day_refire_is_suppressed_by_auto_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])

        hints_first = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert any(h["type"] == "recompose_bootstrap" for h in hints_first)

        # decision数(発火条件)は満たしたままだが、直前の発火で付いた当日付き
        # クールダウンマーカーにより2回目は抑制される
        hints_second = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert not any(h["type"] == "recompose_bootstrap" for h in hints_second)

    def test_next_day_refires_after_cooldown(self, temp_db, monkeypatch):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])

        monkeypatch.setattr(hint_service, "date", _fixed_date(2026, 1, 1))
        hints_day1 = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert any(h["type"] == "recompose_bootstrap" for h in hints_day1)
        assert f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:2026-01-01" in _get_tag_notes(DOMAIN_TAG_NAME)

        monkeypatch.setattr(hint_service, "date", _fixed_date(2026, 1, 2))
        hints_day2 = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert any(h["type"] == "recompose_bootstrap" for h in hints_day2)
        assert f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:2026-01-02" in _get_tag_notes(DOMAIN_TAG_NAME)

    def test_manual_future_marker_is_not_overwritten_by_auto_cooldown(self, temp_db):
        """手動で未来日のuntilマーカーを設定済みの場合、hint自体が抑制され続け、
        そのマーカーの日付もget_hints呼び出しによって書き換えられない"""
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
        update_tag(DOMAIN_TAG, notes=f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:2099-01-01")

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert not any(h["type"] == "recompose_bootstrap" for h in hints)
        assert f"{MARKER_RECOMPOSE_BOOTSTRAP}-until:2099-01-01" in _get_tag_notes(DOMAIN_TAG_NAME)

    def test_direction_overflow_not_subject_to_cooldown(self, temp_db):
        """direction_overflowは日次クールダウンの対象外。連続発火してもマーカーは付かない"""
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(DIRECTION_OVERFLOW_THRESHOLD):
            _add_direction_decision(topic["topic_id"], i)

        get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        hints_second = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert any(h["type"] == "direction_overflow" for h in hints_second)
        assert MARKER_DIRECTION_OVERFLOW not in _get_tag_notes(DOMAIN_TAG_NAME)


class TestCooldownMarkerWriteFailure:
    """tags.notesラチェット天井(migrations/0066)によりクールダウンマーカーの
    追記が失敗しても、計算済みのhintが握りつぶされないことの検証"""

    def test_tag_scope_hint_survives_marker_write_failure(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])

        # notesを天井ちょうど(4000字)にしておく。クールダウンマーカー追記は
        # 必ず4000字を超え、DBトリガーがIntegrityErrorで拒否する状況を作る
        ceiling_notes = "x" * 4000
        result = update_tag(DOMAIN_TAG, notes=ceiling_notes)
        assert "error" not in result, result

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert any(h["type"] == "recompose_bootstrap" for h in hints)
        # マーカー追記は天井超過で失敗しているため、notesは変化しないまま
        assert _get_tag_notes(DOMAIN_TAG_NAME) == ceiling_notes

    def test_activity_scope_other_tag_hint_not_lost_when_one_tag_marker_write_fails(
        self, temp_db
    ):
        """activity scopeの集約で、1タグのマーカー書き込み失敗が他タグ分の
        計算済みhintまで巻き添えで消さないことを確認する"""
        other_tag_name = "hint-domain-other"
        other_tag = f"domain:{other_tag_name}"

        topic1 = add_topic(title="t1", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic1["topic_id"])
        dec0 = add_decision(decision="anchor1", reason="r", topic_id=topic1["topic_id"])

        topic2 = add_topic(title="t2", description="d", tags=[other_tag])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"e{i}", reason="r", topic_id=topic2["topic_id"])
        dec1 = add_decision(decision="anchor2", reason="r", topic_id=topic2["topic_id"])

        # DOMAIN_TAGのnotesだけを天井ちょうどにしておき、そちらのマーカー追記を失敗させる
        result = update_tag(DOMAIN_TAG, notes="x" * 4000)
        assert "error" not in result, result

        activity = add_activity(
            title="[作業] x", description="d",
            tags=[DOMAIN_TAG, other_tag, "intent:implement"],
            related=[
                {"type": "decision", "ids": [dec0["decision_id"], dec1["decision_id"]]},
            ],
            check_in=False,
        )

        hints = get_hints("activity", activity["activity_id"])
        recompose_hints = [h for h in hints if h["type"] == "recompose_bootstrap"]
        sources = {h["source"] for h in recompose_hints}
        assert f"recompose_bootstrap:tag:{_tag_id(DOMAIN_TAG_NAME)}" in sources
        assert f"recompose_bootstrap:tag:{_tag_id(other_tag_name)}" in sources


class TestEdgeCases:
    def test_unknown_scope_returns_empty(self, temp_db):
        conn = get_connection()
        try:
            assert get_hints_with_conn(conn, "tag", 999_999) == []
            assert get_hints_with_conn(conn, "topic", 999_999) == []
            assert get_hints_with_conn(conn, "activity", 999_999) == []
        finally:
            conn.close()

    def test_intent_tag_not_targeted_for_recompose(self, temp_db):
        topic = add_topic(title="t", description="d", tags=["domain:other"])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(
                decision=f"d{i}", reason="r", topic_id=topic["topic_id"],
                tags=["intent:design"],
            )

        intent_tag_id = _tag_id("design", namespace="intent")
        assert get_hints("tag", intent_tag_id) == []


class TestCountStaleActivitiesHelper:
    """_count_stale_activities: 放置件数カウントの母集団・鮮度判定のDB結線テスト"""

    def test_counts_stale_pending_activity_with_null_heartbeat(self, temp_db):
        """last_heartbeat_at IS NULLでもCOALESCEにより正しくカウントされる
        (COALESCEを外すとMAX()の結果がNULLになり判定式から漏れる回帰の防止)"""
        _make_activity_for_cleanup(status="pending")

        conn = get_connection()
        try:
            assert _count_stale_activities(conn) == 1
        finally:
            conn.close()

    def test_excludes_recently_updated_activity(self, temp_db):
        _make_activity_for_cleanup(status="pending", updated_at=None)

        conn = get_connection()
        try:
            assert _count_stale_activities(conn) == 0
        finally:
            conn.close()

    def test_recent_heartbeat_keeps_activity_fresh_despite_old_updated_at(self, temp_db):
        """MAX(updated_at, last_heartbeat_at)。heartbeatの方が新しければ
        updated_atが古くても放置扱いされない"""
        conn = get_connection()
        try:
            now = conn.execute("SELECT datetime('now')").fetchone()[0]
        finally:
            conn.close()

        _make_activity_for_cleanup(
            status="pending", updated_at=STALE_TS, heartbeat_at=now
        )

        conn = get_connection()
        try:
            assert _count_stale_activities(conn) == 0
        finally:
            conn.close()

    def test_boundary_just_under_stale_days_not_counted(self, temp_db):
        """更新6日前(ACTIVITY_CLEANUP_STALE_DAYS=7日の境界より内側)は放置扱いに
        ならない。日数を6/8とハードコードすることで、ACTIVITY_CLEANUP_STALE_DAYS
        の値やSQLの時間単位('days'→'minutes'等)を変えると必ず落ちる境界値テスト
        にする。STALE_TS(2000年)/現在時刻の二択では7日境界そのものは検出できない"""
        conn = get_connection()
        try:
            six_days_ago = conn.execute(
                "SELECT datetime('now', '-6 days')"
            ).fetchone()[0]
        finally:
            conn.close()

        _make_activity_for_cleanup(status="pending", updated_at=six_days_ago)

        conn = get_connection()
        try:
            assert _count_stale_activities(conn) == 0
        finally:
            conn.close()

    def test_boundary_just_over_stale_days_counted(self, temp_db):
        """更新8日前(7日境界より外側)は放置扱いになる。直前のテストと対をなす
        境界値テストで、7日という具体的な閾値の内と外を1日ずつで挟む"""
        conn = get_connection()
        try:
            eight_days_ago = conn.execute(
                "SELECT datetime('now', '-8 days')"
            ).fetchone()[0]
        finally:
            conn.close()

        _make_activity_for_cleanup(status="pending", updated_at=eight_days_ago)

        conn = get_connection()
        try:
            assert _count_stale_activities(conn) == 1
        finally:
            conn.close()

    def test_includes_in_progress_status(self, temp_db):
        _make_activity_for_cleanup(status="in_progress")

        conn = get_connection()
        try:
            assert _count_stale_activities(conn) == 1
        finally:
            conn.close()

    def test_includes_shelved_status(self, temp_db):
        _make_activity_for_cleanup(status="shelved")

        conn = get_connection()
        try:
            assert _count_stale_activities(conn) == 1
        finally:
            conn.close()

    def test_includes_snoozed_status(self, temp_db):
        _make_activity_for_cleanup(status="snoozed")

        conn = get_connection()
        try:
            assert _count_stale_activities(conn) == 1
        finally:
            conn.close()

    def test_excludes_completed_status(self, temp_db):
        _make_activity_for_cleanup(status="completed")

        conn = get_connection()
        try:
            assert _count_stale_activities(conn) == 0
        finally:
            conn.close()

    def test_includes_orch_managed_activity(self, temp_db):
        """orch_managedは旧ow運用体系の名残の死んだカラムであり、本判定の
        母集団フィルタには使わない。orch_managed=Trueのactivityも他と同様に
        カウント対象に含まれる(母集団はstatusのみで判定するユーザー裁定)"""
        _make_activity_for_cleanup(status="pending", orch_managed=True)

        conn = get_connection()
        try:
            assert _count_stale_activities(conn) == 1
        finally:
            conn.close()


class TestActivityCleanupHint:
    """scope=activityで発火するactivity_cleanup hintの統合テスト。

    activity_cleanupはこのactivity固有のtagとは無関係なシステム全体判定のため、
    どのactivity_idに対してget_hintsを呼んでも同じ判定結果になる。
    """

    def test_fires_at_threshold(self, temp_db):
        """放置件数がちょうど閾値のときにfireする(>=判定の境界確認)"""
        _ensure_activity_management_tag()
        activity_ids = [
            _make_activity_for_cleanup() for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD)
        ]

        hints = get_hints("activity", activity_ids[0])
        cleanup_hints = [h for h in hints if h["type"] == "activity_cleanup"]
        assert len(cleanup_hints) == 1
        assert cleanup_hints[0]["severity"] == "info"
        assert cleanup_hints[0]["delivery_hint"] == "immediate"
        assert str(ACTIVITY_CLEANUP_COUNT_THRESHOLD) in cleanup_hints[0]["message"]

    def test_suggested_action_names_activity_cleanup_skill(self, temp_db):
        _ensure_activity_management_tag()
        activity_ids = [
            _make_activity_for_cleanup() for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD)
        ]

        hints = get_hints("activity", activity_ids[0])
        cleanup_hint = next(h for h in hints if h["type"] == "activity_cleanup")
        assert cleanup_hint["suggested_action"]["skill"] == "activity-cleanup"

    def test_silent_below_threshold(self, temp_db):
        """閾値未満(threshold-1件)ではfireしない"""
        _ensure_activity_management_tag()
        activity_ids = [
            _make_activity_for_cleanup()
            for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD - 1)
        ]

        hints = get_hints("activity", activity_ids[0])
        assert not any(h["type"] == "activity_cleanup" for h in hints)

    def test_suppressed_by_permanent_marker(self, temp_db):
        _ensure_activity_management_tag()
        activity_ids = [
            _make_activity_for_cleanup() for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD)
        ]
        update_tag(ACTIVITY_MANAGEMENT_TAG, notes=MARKER_ACTIVITY_CLEANUP)

        hints = get_hints("activity", activity_ids[0])
        assert not any(h["type"] == "activity_cleanup" for h in hints)

    def test_suppressed_by_future_dated_marker(self, temp_db):
        _ensure_activity_management_tag()
        activity_ids = [
            _make_activity_for_cleanup() for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD)
        ]
        update_tag(
            ACTIVITY_MANAGEMENT_TAG, notes=f"{MARKER_ACTIVITY_CLEANUP}-until:2099-01-01"
        )

        hints = get_hints("activity", activity_ids[0])
        assert not any(h["type"] == "activity_cleanup" for h in hints)

    def test_refires_after_dated_marker_expires(self, temp_db):
        _ensure_activity_management_tag()
        activity_ids = [
            _make_activity_for_cleanup() for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD)
        ]
        update_tag(
            ACTIVITY_MANAGEMENT_TAG, notes=f"{MARKER_ACTIVITY_CLEANUP}-until:2000-01-01"
        )

        hints = get_hints("activity", activity_ids[0])
        assert any(h["type"] == "activity_cleanup" for h in hints)

    def test_silent_when_activity_management_tag_not_created(self, temp_db):
        """activity-managementタグ自体が存在しない場合は判定不能として静かにスキップする
        (恒久沈黙ではなく、タグ作成後は通常通り判定対象になる)"""
        activity_ids = [
            _make_activity_for_cleanup() for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD)
        ]

        hints = get_hints("activity", activity_ids[0])
        assert not any(h["type"] == "activity_cleanup" for h in hints)

    def test_fire_appends_exactly_one_daily_cooldown_marker(self, temp_db):
        """発火するとactivity-managementタグのnotesに当日日付の日次クールダウン
        マーカーが1つだけ追記される。マーカー出現数を1に固定することで、
        7日等の長期マーカーがhint_service側で別途書き込まれていないこと
        (skill実行完了時の期限付きマーカー書き込みはskill側の責務)を検証する"""
        _ensure_activity_management_tag()
        activity_ids = [
            _make_activity_for_cleanup() for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD)
        ]

        hints = get_hints("activity", activity_ids[0])
        assert any(h["type"] == "activity_cleanup" for h in hints)

        today = date.today().isoformat()
        notes = _get_tag_notes(ACTIVITY_MANAGEMENT_TAG_NAME, namespace="")
        assert f"{MARKER_ACTIVITY_CLEANUP}-until:{today}" in notes
        assert notes.count(f"{MARKER_ACTIVITY_CLEANUP}-until:") == 1

    def test_same_day_refire_is_suppressed_by_auto_marker(self, temp_db):
        _ensure_activity_management_tag()
        activity_ids = [
            _make_activity_for_cleanup() for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD)
        ]

        hints_first = get_hints("activity", activity_ids[0])
        assert any(h["type"] == "activity_cleanup" for h in hints_first)

        hints_second = get_hints("activity", activity_ids[0])
        assert not any(h["type"] == "activity_cleanup" for h in hints_second)

    def test_not_surfaced_via_tag_scope(self, temp_db):
        """activity_cleanupはactivity scope経由でのみ発火する。tag scopeで
        activity-managementタグ自体を問い合わせても現れない"""
        _ensure_activity_management_tag()
        for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD):
            _make_activity_for_cleanup()

        hints = get_hints("tag", _tag_id(ACTIVITY_MANAGEMENT_TAG_NAME, namespace=""))
        assert not any(h["type"] == "activity_cleanup" for h in hints)

    def test_not_surfaced_via_topic_scope(self, temp_db):
        _ensure_activity_management_tag()
        for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD):
            _make_activity_for_cleanup()
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])

        hints = get_hints("topic", topic["topic_id"])
        assert not any(h["type"] == "activity_cleanup" for h in hints)

    def test_namespace_scoped_same_name_tag_does_not_interfere(self, temp_db):
        """将来`domain:activity-management`のような別namespaceの同名タグが
        作られても、素タグ`activity-management`限定のマーカー解決には
        影響しないことを、get_hintsを通した挙動として確認する。素タグを
        先に作成(無印のまま)し、その後デコイに恒久抑制マーカーを付けても、
        放置件数が閾値以上ならヒントは発火する。

        本テストは素タグを先に作る順序に固定しているため、namespace絞り込み
        自体が実装から失われても検出できない(rowid順のフルスキャンでは
        素タグが先に返るため)。namespace絞り込みのプラン非依存な回帰検出は
        test_get_tag_id_and_notes_returns_none_when_only_namespaced_decoy_exists
        が担う。"""
        _ensure_activity_management_tag()
        decoy_tag = f"domain:{ACTIVITY_MANAGEMENT_TAG_NAME}"
        add_topic(title="decoy", description="d", tags=[decoy_tag])
        update_tag(decoy_tag, notes=MARKER_ACTIVITY_CLEANUP)

        activity_ids = [
            _make_activity_for_cleanup() for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD)
        ]

        hints = get_hints("activity", activity_ids[0])
        assert any(h["type"] == "activity_cleanup" for h in hints)

    @pytest.mark.parametrize("decoy_first", [True, False])
    def test_get_tag_id_and_notes_scopes_by_namespace_regardless_of_creation_order(
        self, temp_db, decoy_first
    ):
        """`_get_tag_id_and_notes`は、namespace=''の素タグと
        `domain:activity-management`のデコイが両方存在する状態で、
        どちらを先に作っても素タグ側のid・空文字notesだけを返すことを確認する。

        素タグとデコイが両方存在するこの構成では、namespace絞り込みを外した
        場合にどちらの行が返るかがSQLiteのクエリプラン(select listに`notes`が
        含まれる現状はフルスキャン+rowid順、含まれなければ`(namespace, name)`の
        covering indexでnamespace=''が先頭)に左右されるため、生成順序をどう
        振っても回帰を確実には検出できない。namespace絞り込み自体のプラン非依存な
        回帰検出は
        test_get_tag_id_and_notes_returns_none_when_only_namespaced_decoy_exists
        が担う。本テストは両順序で正しい挙動になることの確認に限定する。"""
        decoy_tag = f"domain:{ACTIVITY_MANAGEMENT_TAG_NAME}"

        def _make_decoy() -> None:
            add_topic(title="decoy", description="d", tags=[decoy_tag])
            update_tag(decoy_tag, notes=MARKER_ACTIVITY_CLEANUP)

        if decoy_first:
            _make_decoy()
            _ensure_activity_management_tag()
        else:
            _ensure_activity_management_tag()
            _make_decoy()

        plain_tag_id = _tag_id(ACTIVITY_MANAGEMENT_TAG_NAME, namespace="")

        conn = get_connection()
        try:
            tag_id, notes = hint_service._get_tag_id_and_notes(
                conn, ACTIVITY_MANAGEMENT_TAG_NAME
            )
        finally:
            conn.close()

        assert tag_id == plain_tag_id
        assert notes == ""

    def test_get_tag_id_and_notes_returns_none_when_only_namespaced_decoy_exists(
        self, temp_db
    ):
        """namespace=''の素タグ`activity-management`を一切作らず、
        `domain:activity-management`というデコイだけを作った状態で
        `_get_tag_id_and_notes`を呼ぶと(None, "")が返ることを確認する。

        この構成では`name = ?`に一致する行がテーブル全体でデコイの1行のみに
        なるため、SQLiteがフルスキャン・covering indexのどちらを選んでも
        結果は変わらない。namespace = ''の絞り込みが実装から失われると、
        この唯一の一致行(namespace='domain'側)がそのまま返ってしまい、
        tag_idは非None・notesにはデコイへ付けたマーカー文字列が入るため、
        本テストはクエリプランに依存せず決定的にこの回帰を検出できる。"""
        decoy_tag = f"domain:{ACTIVITY_MANAGEMENT_TAG_NAME}"
        add_topic(title="decoy", description="d", tags=[decoy_tag])
        update_tag(decoy_tag, notes=MARKER_ACTIVITY_CLEANUP)

        conn = get_connection()
        try:
            tag_id, notes = hint_service._get_tag_id_and_notes(
                conn, ACTIVITY_MANAGEMENT_TAG_NAME
            )
        finally:
            conn.close()

        assert (tag_id, notes) == (None, "")

    def test_source_follows_type_scope_id_convention(self, temp_db):
        """sourceは他hint種別と同じ`{type}:{scope}:{id}`形式に揃える。
        activity_cleanupはグローバル判定だが、マーカーを置くtag_idをidとして使う"""
        _ensure_activity_management_tag()
        activity_ids = [
            _make_activity_for_cleanup() for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD)
        ]
        am_tag_id = _tag_id(ACTIVITY_MANAGEMENT_TAG_NAME, namespace="")

        hints = get_hints("activity", activity_ids[0])
        cleanup_hint = next(h for h in hints if h["type"] == "activity_cleanup")
        assert cleanup_hint["source"] == f"activity_cleanup:tag:{am_tag_id}"

    def test_same_result_regardless_of_queried_activity_tag_composition(self, temp_db):
        """activity_cleanupはこのactivity固有のtagとは無関係なグローバル判定
        であり、どのactivity_idに対してget_hints_with_connを呼んでも
        同じ判定結果になる。

        「同じタグを持つ2つのactivity同士」を比較するだけでは、
        `_get_hints_for_activity`が例えば`cleanup_hint = _get_activity_cleanup_hint(conn)
        if rows else None`のようにdomain:tagの有無に依存する実装へ変異しても
        検出できない。これを検出するため、domain:tagを持つactivity(rowsが
        非空になる)と、domain:tagを一切持たないactivity(rowsが空になる)を
        意図的に比較する。

        get_hints_with_connはcommitしないため、各呼び出し後にrollbackすることで
        1回目の発火によるクールダウンマーカー書き込みを2回目の判定に持ち越さない"""
        _ensure_activity_management_tag()
        activity_with_domain_tag = _make_activity_for_cleanup()
        for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD - 1):
            _make_activity_for_cleanup()
        # domain:namespaceのtagを一切持たないactivity。_get_hints_for_activityの
        # domain:tag展開ループでは`rows`が空になるが、放置件数の母集団判定
        # (_count_stale_activities)自体はtagを見ないため、この行も母集団には含まれる。
        activity_without_domain_tag = _make_activity_for_cleanup(tag="no-domain-tag")

        conn = get_connection()
        try:
            hints_with_domain_tag = get_hints_with_conn(
                conn, "activity", activity_with_domain_tag
            )
        finally:
            conn.rollback()
            conn.close()

        conn = get_connection()
        try:
            hints_without_domain_tag = get_hints_with_conn(
                conn, "activity", activity_without_domain_tag
            )
        finally:
            conn.rollback()
            conn.close()

        assert any(h["type"] == "activity_cleanup" for h in hints_with_domain_tag)
        assert any(h["type"] == "activity_cleanup" for h in hints_without_domain_tag)


class TestActivityCleanupMessageHelper:
    """_activity_cleanup_message: 催促文言の純粋関数テスト（DB不要）"""

    def test_includes_count(self):
        message = hint_service._activity_cleanup_message(42)
        assert "42" in message

    def test_message_includes_autotrigger_guard(self):
        message = hint_service._activity_cleanup_message(42)
        assert ACTIVITY_CLEANUP_AUTOTRIGGER_GUARD in message

    def test_includes_snooze_instructions(self):
        message = hint_service._activity_cleanup_message(42)
        assert "-until:" in message
        assert MARKER_ACTIVITY_CLEANUP in message

    def test_avoids_heavy_wording(self):
        """エッジケース#11: 「専属セッションを立てて」のような重い誘導文言を
        含まない。実装が一度もそのフレーズを持ったことがない場合、この
        非存在アサーション自体の回帰検出力はゼロである。
        受け入れ基準のエッジケース表#11に対応するテストを存在させるために
        残す(意図的に弱いテストとして維持)。"""
        message = hint_service._activity_cleanup_message(42)
        assert "専属セッション" not in message
