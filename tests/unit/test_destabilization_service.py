"""destabilization_service のテスト

resolve_destabilizationの3分岐（reaffirmed/revised/retracted）、
revised_to_decision_id必須バリデーション、冪等性をカバーする。
"""
import os
import tempfile
import pytest

from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.decision_service import add_decisions
from src.services.destabilization_service import resolve_destabilization
from src.services.tag_service import _injected_tags


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
def decisions(temp_db):
    """テスト用decisionを3件作成する（source=軸変更、target=影響先、revised_to=新結論）"""
    topic = add_topic(title="テストトピック", description="テスト用", tags=DEFAULT_TAGS)
    tid = topic["topic_id"]
    result = add_decisions([
        {"topic_id": tid, "decision": "軸変更decision", "reason": "軸変更理由"},
        {"topic_id": tid, "decision": "影響先decision", "reason": "影響先理由"},
        {"topic_id": tid, "decision": "改訂後decision", "reason": "改訂後理由"},
    ])
    created = result["created"]
    return {
        "source_id": created[0]["decision_id"],
        "target_id": created[1]["decision_id"],
        "revised_to_id": created[2]["decision_id"],
    }


def _get_resolution_row(source_id: int, target_id: int):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT resolution, revised_to_decision_id, note FROM decision_destabilization_resolutions "
            "WHERE source_id = ? AND target_id = ?",
            (source_id, target_id),
        ).fetchone()
    finally:
        conn.close()


def _get_retracted_at(decision_id: int):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT retracted_at FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        return row["retracted_at"]
    finally:
        conn.close()


class TestResolveDestabilizationValidation:
    """引数バリデーション"""

    def test_invalid_resolution_value_rejected(self, decisions):
        """resolutionが3値以外だとVALIDATION_ERRORになり、行は追加されない"""
        d = decisions
        result = resolve_destabilization(d["source_id"], d["target_id"], "bogus")
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert _get_resolution_row(d["source_id"], d["target_id"]) is None

    def test_revised_without_revised_to_decision_id_rejected(self, decisions):
        """resolution='revised'でrevised_to_decision_id未指定だとVALIDATION_ERRORになり、行は追加されない"""
        d = decisions
        result = resolve_destabilization(d["source_id"], d["target_id"], "revised")
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert _get_resolution_row(d["source_id"], d["target_id"]) is None

    def test_reaffirmed_nonexistent_target_rejected(self, decisions):
        """reaffirmedで存在しないtarget_decision_idを指定するとFK制約違反でCONSTRAINT_VIOLATIONになり、行は追加されない"""
        d = decisions
        result = resolve_destabilization(d["source_id"], 999999, "reaffirmed")
        assert "error" in result
        assert result["error"]["code"] == "CONSTRAINT_VIOLATION"
        assert _get_resolution_row(d["source_id"], 999999) is None

    def test_retracted_nonexistent_target_rejected(self, decisions):
        """retractedで存在しないtarget_decision_idを指定すると、retract経路のnot-foundでITEM_ERRORになり、行は追加されない"""
        d = decisions
        result = resolve_destabilization(d["source_id"], 999999, "retracted")
        assert "error" in result
        assert result["error"]["code"] == "ITEM_ERROR"
        assert _get_resolution_row(d["source_id"], 999999) is None

    def test_retracted_nonexistent_source_rejected_without_retracting_target(self, decisions):
        """retractedで存在しないsource_decision_idを指定すると、targetをretractする前にCONSTRAINT_VIOLATIONで拒否される"""
        d = decisions
        result = resolve_destabilization(999999, d["target_id"], "retracted")
        assert "error" in result
        assert result["error"]["code"] == "CONSTRAINT_VIOLATION"
        assert _get_resolution_row(999999, d["target_id"]) is None
        # targetは実際にはretractされていない（副作用が発生する前に検証で止まる）
        assert _get_retracted_at(d["target_id"]) is None


class TestResolveDestabilizationReaffirmed:
    """resolution='reaffirmed'"""

    def test_reaffirmed_inserts_row_and_does_not_retract_target(self, decisions):
        """reaffirmedはresolution行のみINSERTし、targetのretracted_atは変化しない"""
        d = decisions
        result = resolve_destabilization(
            d["source_id"], d["target_id"], "reaffirmed", note="再確認した"
        )
        assert "error" not in result
        assert result == {"resolved": True, "already_resolved": False}

        row = _get_resolution_row(d["source_id"], d["target_id"])
        assert row is not None
        assert row["resolution"] == "reaffirmed"
        assert row["revised_to_decision_id"] is None
        assert row["note"] == "再確認した"

        assert _get_retracted_at(d["target_id"]) is None


class TestResolveDestabilizationRevised:
    """resolution='revised'"""

    def test_revised_inserts_row_with_revised_to_and_does_not_retract_target(self, decisions):
        """revisedはrevised_to_decision_idを記録し、targetのretracted_atは変化しない"""
        d = decisions
        result = resolve_destabilization(
            d["source_id"], d["target_id"], "revised",
            revised_to_decision_id=d["revised_to_id"],
        )
        assert "error" not in result
        assert result == {"resolved": True, "already_resolved": False}

        row = _get_resolution_row(d["source_id"], d["target_id"])
        assert row is not None
        assert row["resolution"] == "revised"
        assert row["revised_to_decision_id"] == d["revised_to_id"]

        assert _get_retracted_at(d["target_id"]) is None


class TestResolveDestabilizationRetracted:
    """resolution='retracted'"""

    def test_retracted_inserts_row_and_retracts_target(self, decisions):
        """retractedはresolution行をINSERTし、targetを実際にretractする"""
        d = decisions
        result = resolve_destabilization(d["source_id"], d["target_id"], "retracted")
        assert "error" not in result
        assert result == {"resolved": True, "already_resolved": False}

        row = _get_resolution_row(d["source_id"], d["target_id"])
        assert row is not None
        assert row["resolution"] == "retracted"

        assert _get_retracted_at(d["target_id"]) is not None


class TestResolveDestabilizationIdempotent:
    """同一エッジへの二重resolve"""

    def test_second_call_reports_already_resolved_without_duplicate_insert(self, decisions):
        """同一(source, target)への2回目の呼び出しはPK重複INSERTを起こさずalready_resolved=trueを返す"""
        d = decisions
        first = resolve_destabilization(d["source_id"], d["target_id"], "reaffirmed")
        assert first == {"resolved": True, "already_resolved": False}

        second = resolve_destabilization(d["source_id"], d["target_id"], "reaffirmed")
        assert "error" not in second
        assert second == {"resolved": False, "already_resolved": True}

        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM decision_destabilization_resolutions "
                "WHERE source_id = ? AND target_id = ?",
                (d["source_id"], d["target_id"]),
            ).fetchone()["cnt"]
            assert count == 1
        finally:
            conn.close()

    def test_second_call_with_retracted_does_not_re_trigger_retract_side_effect(self, decisions):
        """既にreaffirmed済みのエッジに対しretractedで再度呼んでも、targetは実際にはretractされない"""
        d = decisions
        resolve_destabilization(d["source_id"], d["target_id"], "reaffirmed")

        result = resolve_destabilization(d["source_id"], d["target_id"], "retracted")
        assert result == {"resolved": False, "already_resolved": True}

        # 既存行のresolutionは'reaffirmed'のまま（'retracted'に書き換わらない）
        row = _get_resolution_row(d["source_id"], d["target_id"])
        assert row["resolution"] == "reaffirmed"
        # targetは実際にはretractされていない
        assert _get_retracted_at(d["target_id"]) is None
