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
    DIRECTION_OVERFLOW_THRESHOLD,
    HINT_LOGS_SPARSE_MESSAGE,
    LOGS_SPARSE_LOG_THRESHOLD,
    MARKER_DIRECTION_OVERFLOW,
    MARKER_LOGS_SPARSE,
    MARKER_RECOMPOSE_BOOTSTRAP,
    MARKER_RECOMPOSE_DELTA,
    MARKER_RECOMPOSE_GENERIC,
    RECOMPOSE_AUTOTRIGGER_GUARD,
    RECOMPOSE_BOOTSTRAP_THRESHOLD,
    RECOMPOSE_DELTA_THRESHOLD,
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
