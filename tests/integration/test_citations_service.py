"""citations_service / citation_renderer の統合テスト

write hook (add_*/update_*) と read tool (get_*) を通した実 DB シナリオ。
"""
import os
import tempfile
import pytest

from src.db import get_connection, init_database
from src.services.activity_service import add_activity, update_activity
from src.services.material_service import add_material, get_material, update_material
from src.services.decision_service import add_decisions
from src.services.discussion_log_service import add_logs
from src.services.topic_service import add_topic
from src.services.retract_service import retract
from src.services import citation_renderer

DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def topic_id(temp_db):
    return add_topic(
        title="Topic", description="desc", tags=DEFAULT_TAGS,
    )["topic_id"]


@pytest.fixture
def activity_id(temp_db):
    return add_activity(
        title="Test Activity", description="for tests", tags=DEFAULT_TAGS,
        check_in=False,
    )["activity_id"]


def _citations_rows(owner_type: str, owner_id: int) -> list:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT target_type, target_id, occurrence FROM citations "
            "WHERE owner_type = ? AND owner_id = ? ORDER BY occurrence",
            (owner_type, owner_id),
        ).fetchall()
        return [tuple(r) for r in rows]
    finally:
        conn.close()


class TestAddMaterialCitations:
    def test_add_material_inserts_citation_rows(self, temp_db, activity_id):
        # target material を1個先に作る
        target = add_material(
            title="target", content="body", tags=DEFAULT_TAGS, source="t",
            related=[{"type": "activity", "ids": [activity_id]}],
        )
        target_id = target["material_id"]
        # 本文に cite を含む material を作る
        m = add_material(
            title="owner", content=f"see {{{{cite:M#{target_id}}}}} please",
            tags=DEFAULT_TAGS, source="t",
            related=[{"type": "activity", "ids": [activity_id]}],
        )
        owner_id = m["material_id"]
        assert _citations_rows("material", owner_id) == [("material", target_id, 1)]

    def test_no_cite_no_rows(self, temp_db, activity_id):
        m = add_material(
            title="t", content="no cite here", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )
        assert _citations_rows("material", m["material_id"]) == []


class TestUpdateMaterialCitations:
    def test_update_material_replaces_citations(self, temp_db, activity_id):
        t1 = add_material(
            title="t1", content="b", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        t2 = add_material(
            title="t2", content="b", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        owner = add_material(
            title="owner", content=f"cite {{{{cite:M#{t1}}}}}",
            tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        assert _citations_rows("material", owner) == [("material", t1, 1)]
        # 本文書き換えで t2 を参照に変更
        update_material(owner, content=f"now {{{{cite:M#{t2}}}}}")
        assert _citations_rows("material", owner) == [("material", t2, 1)]

    def test_update_material_title_only_replays(self, temp_db, activity_id):
        t1 = add_material(
            title="t1", content="b", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        owner = add_material(
            title="owner", content=f"cite {{{{cite:M#{t1}}}}}",
            tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        before = _citations_rows("material", owner)
        update_material(owner, title="new title")
        after = _citations_rows("material", owner)
        # 本文無変更でも再投入が走り、参照リストは同等
        assert before == after


class TestUpdateActivityCitations:
    def test_update_activity_replaces_citations(self, temp_db, activity_id):
        t1 = add_material(
            title="t1", content="b", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        update_activity(
            activity_id, description=f"now refer {{{{cite:M#{t1}}}}}",
        )
        assert _citations_rows("activity", activity_id) == [("material", t1, 1)]


class TestAddDecisionsCitations:
    def test_decision_cite_inserted(self, temp_db, topic_id, activity_id):
        t = add_material(
            title="t", content="b", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        res = add_decisions([{
            "topic_id": topic_id,
            "decision": f"adopt {{{{cite:M#{t}}}}}",
            "reason": "based on findings",
        }])
        decision_id = res["created"][0]["decision_id"]
        assert _citations_rows("decision", decision_id) == [("material", t, 1)]


class TestAddLogsCitations:
    def test_log_cite_inserted(self, temp_db, topic_id, activity_id):
        t = add_material(
            title="t", content="b", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        res = add_logs([{
            "topic_id": topic_id,
            "title": "L",
            "content": f"discussion of {{{{cite:M#{t}}}}}",
        }])
        log_id = res["created"][0]["log_id"]
        assert _citations_rows("log", log_id) == [("material", t, 1)]


class TestAddTopicCitations:
    def test_topic_cite_inserted(self, temp_db, activity_id):
        t = add_material(
            title="t", content="b", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        new_topic = add_topic(
            title=f"refer {{{{cite:M#{t}}}}}",
            description="topic desc",
            tags=DEFAULT_TAGS,
        )
        topic_id = new_topic["topic_id"]
        assert _citations_rows("topic", topic_id) == [("material", t, 1)]


class TestOwnerCascade:
    def test_owner_delete_cascades_citations(self, temp_db, activity_id):
        t = add_material(
            title="t", content="b", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        owner = add_material(
            title="owner", content=f"see {{{{cite:M#{t}}}}}",
            tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        assert _citations_rows("material", owner) == [("material", t, 1)]
        # owner を物理削除 → trigger でカスケード削除
        conn = get_connection()
        with conn:
            conn.execute("DELETE FROM materials WHERE id = ?", (owner,))
        conn.close()
        assert _citations_rows("material", owner) == []


class TestTargetRetractStaysButRendersDangling:
    def test_target_retract_leaves_citations_row(self, temp_db, topic_id, activity_id):
        # decision を作って citation の target にする
        target_dec = add_decisions([{
            "topic_id": topic_id,
            "decision": "to be retracted",
            "reason": "x",
            "title": "target dec",
        }])["created"][0]["decision_id"]
        owner = add_material(
            title="owner",
            content=f"adopt {{{{cite:D#{target_dec}}}}}",
            tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        # target を retract
        retract("decision", [target_dec])
        # citations 行は残る
        assert _citations_rows("material", owner) == [("decision", target_dec, 1)]
        # renderer で [retracted] 表示
        from src.services.citation_renderer import expand
        conn = get_connection()
        try:
            out = expand(f"see {{{{cite:D#{target_dec}}}}}", "internal", conn)
            assert f"[retracted D#{target_dec}]" in out
        finally:
            conn.close()


class TestGetMaterialFlavor:
    def test_get_material_internal_default(self, temp_db, activity_id):
        from src.main import get_material as tool_get_material
        t = add_material(
            title="t", content="b", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        owner = add_material(
            title="o", content=f"see {{{{cite:M#{t}}}}}",
            tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        out = tool_get_material(owner)
        assert "(M#" + str(t) + ")" in out["content"]
        assert "citations_in" in out
        assert "citations_out" in out
        assert out["citations_out"] == [{"type": "material", "id": t, "title": "t"}]

    def test_get_material_raw(self, temp_db, activity_id):
        from src.main import get_material as tool_get_material
        t = add_material(
            title="t", content="b", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        owner = add_material(
            title="o", content=f"see {{{{cite:M#{t}}}}}",
            tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        out = tool_get_material(owner, flavor="raw")
        # raw は無加工
        assert f"{{{{cite:M#{t}}}}}" in out["content"]

    def test_get_material_readable(self, temp_db, activity_id):
        from src.main import get_material as tool_get_material
        t = add_material(
            title="t", content="b", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        owner = add_material(
            title="o", content=f"see {{{{cite:M#{t}}}}}",
            tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        out = tool_get_material(owner, flavor="readable")
        assert "(M#" + str(t) + ")" not in out["content"]
        assert " t" in out["content"]  # title だけ展開


class TestCitationsInOut:
    def test_distinct_dedupes_multiple_occurrences(self, temp_db, activity_id):
        t = add_material(
            title="t", content="b", tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        owner = add_material(
            title="o",
            content=f"first {{{{cite:M#{t}}}}} second {{{{cite:M#{t}}}}}",
            tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        from src.services.citations_service import _get_in_out_with_conn
        conn = get_connection()
        try:
            io = _get_in_out_with_conn(conn, "material", owner)
        finally:
            conn.close()
        # owner 側 citations_out は DISTINCT で 1 件
        assert io["out"] == [{"type": "material", "id": t, "title": "t"}]

    def test_deleted_target_meta(self, temp_db, activity_id):
        owner = add_material(
            title="o", content="see {{cite:M#9999}}",
            tags=DEFAULT_TAGS, source="x",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        from src.services.citations_service import _get_in_out_with_conn
        conn = get_connection()
        try:
            io = _get_in_out_with_conn(conn, "material", owner)
        finally:
            conn.close()
        assert io["out"] == [
            {"type": "material", "id": 9999, "title": None, "deleted": True}
        ]
