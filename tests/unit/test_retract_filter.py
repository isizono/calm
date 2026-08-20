"""retractフィルタのテスト

get_decisions, get_logs, check_in, searchで
retractされたエンティティがデフォルト除外されることを確認する。
"""
import os
import tempfile
import pytest

from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.discussion_log_service import add_logs, get_logs
from src.services.decision_service import add_decisions, get_decisions
from src.services.retract_service import retract
from src.services.checkin_service import check_in
from src.services.activity_service import add_activity
from src.services.relation_service import add_relation
from src.services.tag_service import _injected_tags
from src.services.pin_service import add_pin


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
def topic(temp_db):
    """テスト用トピックを作成する"""
    return add_topic(title="テストトピック", description="テスト用", tags=DEFAULT_TAGS)


@pytest.fixture
def activity_with_topic(topic):
    """テスト用アクティビティとトピックを作成しリレーションを張る"""
    tid = topic["topic_id"]
    act = add_activity(title="テストアクティビティ", description="テスト用", tags=DEFAULT_TAGS)
    aid = act["activity_id"]
    add_relation("activity", aid, [{"type": "topic", "ids": [tid]}])
    return {"topic_id": tid, "activity_id": aid}


class TestGetDecisionsFilter:
    """get_decisionsのretractフィルタ"""

    def test_retracted_excluded_by_default(self, topic):
        """retractされたdecisionはデフォルトで除外される"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "有効な決定", "reason": "理由1"},
            {"topic_id": tid, "decision": "取り消す決定", "reason": "理由2"},
        ])
        retracted_id = result["created"][1]["decision_id"]

        retract("decision", [retracted_id])

        decisions = get_decisions("topic", tid)
        ids = [d["id_raw"] for d in decisions["decisions"]]
        assert retracted_id not in ids
        assert len(decisions["decisions"]) == 1

    def test_include_retracted_true(self, topic):
        """include_retracted=Trueでretracted含む"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "有効な決定", "reason": "理由1"},
            {"topic_id": tid, "decision": "取り消す決定", "reason": "理由2"},
        ])
        retracted_id = result["created"][1]["decision_id"]

        retract("decision", [retracted_id])

        decisions = get_decisions("topic", tid, include_retracted=True)
        ids = [d["id_raw"] for d in decisions["decisions"]]
        assert retracted_id in ids
        assert len(decisions["decisions"]) == 2


class TestGetLogsFilter:
    """get_logsのretractフィルタ"""

    def test_retracted_excluded_by_default(self, topic):
        """retractされたlogはデフォルトで除外される"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "有効なログ", "title": "ログ1"},
            {"topic_id": tid, "content": "取り消すログ", "title": "ログ2"},
        ])
        retracted_id = result["created"][1]["log_id"]

        retract("log", [retracted_id])

        logs = get_logs("topic", tid)
        ids = [l["id_raw"] for l in logs["logs"]]
        assert retracted_id not in ids
        assert len(logs["logs"]) == 1

    def test_include_retracted_true(self, topic):
        """include_retracted=Trueでretracted含む"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "有効なログ", "title": "ログ1"},
            {"topic_id": tid, "content": "取り消すログ", "title": "ログ2"},
        ])
        retracted_id = result["created"][1]["log_id"]

        retract("log", [retracted_id])

        logs = get_logs("topic", tid, include_retracted=True)
        ids = [l["id_raw"] for l in logs["logs"]]
        assert retracted_id in ids
        assert len(logs["logs"]) == 2


class TestCheckInFilter:
    """check_inのretractフィルタ"""

    def test_retracted_decision_excluded_from_checkin(self, activity_with_topic):
        """retractされたdecisionはcheck-inのrecent_decisionsに含まれない"""
        tid = activity_with_topic["topic_id"]
        aid = activity_with_topic["activity_id"]

        result = add_decisions([
            {"topic_id": tid, "decision": "有効な決定", "reason": "理由"},
            {"topic_id": tid, "decision": "取り消す決定", "reason": "理由"},
        ])
        retracted_id = result["created"][1]["decision_id"]

        retract("decision", [retracted_id])

        checkin = check_in(aid)
        assert "error" not in checkin
        decision_ids = [d["id_raw"] for d in checkin.get("recent_decisions", [])]
        assert retracted_id not in decision_ids

    def test_retracted_log_excluded_from_checkin(self, activity_with_topic):
        """retractされたlogはcheck-inのlatest_log/logsに含まれない"""
        tid = activity_with_topic["topic_id"]
        aid = activity_with_topic["activity_id"]

        result = add_logs([
            {"topic_id": tid, "content": "有効なログ", "title": "ログ1"},
            {"topic_id": tid, "content": "取り消すログ（最新）", "title": "ログ2"},
        ])
        retracted_id = result["created"][1]["log_id"]
        valid_id = result["created"][0]["log_id"]

        # 最新のログをretract
        retract("log", [retracted_id])

        checkin = check_in(aid)
        assert "error" not in checkin

        # latest_logがretractされたものではないことを確認
        if checkin.get("latest_log"):
            assert checkin["latest_log"]["id_raw"] != retracted_id

    def test_pinned_retracted_decision_excluded_from_checkin(self, activity_with_topic):
        """pinsテーブルでpinされたdecisionをretractすると、check-inのpinnedに含まれない"""
        tid = activity_with_topic["topic_id"]
        aid = activity_with_topic["activity_id"]

        result = add_decisions([
            {"topic_id": tid, "decision": "pinしてretractする決定", "reason": "理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        # pinsテーブルに登録 → retract
        add_pin("activity", aid, "decision", decision_id)
        retract("decision", [decision_id])

        checkin = check_in(aid)
        assert "error" not in checkin

        # retractされているためpinnedセクションに含まれない
        pinned = checkin.get("pinned", {})
        pinned_decision_ids = [d["id_raw"] for d in pinned.get("decisions", [])]
        assert decision_id not in pinned_decision_ids

    def test_pinned_retracted_log_excluded_from_checkin(self, activity_with_topic):
        """pinsテーブルでpinされたlogをretractすると、check-inのpinnedに含まれない"""
        tid = activity_with_topic["topic_id"]
        aid = activity_with_topic["activity_id"]

        result = add_logs([
            {"topic_id": tid, "content": "pinしてretractするログ", "title": "ログ"},
        ])
        log_id = result["created"][0]["log_id"]

        # pinsテーブルに登録 → retract
        add_pin("activity", aid, "log", log_id)
        retract("log", [log_id])

        checkin = check_in(aid)
        assert "error" not in checkin

        # retractされているためpinnedセクションに含まれない
        pinned = checkin.get("pinned", {})
        pinned_log_ids = [l["id_raw"] for l in pinned.get("logs", [])]
        assert log_id not in pinned_log_ids

    def test_retracted_excluded_from_count(self, activity_with_topic):
        """retractされたdecisionはcoverage分母のカウントに含まれない"""
        tid = activity_with_topic["topic_id"]
        aid = activity_with_topic["activity_id"]

        result = add_decisions([
            {"topic_id": tid, "decision": "有効な決定", "reason": "理由"},
            {"topic_id": tid, "decision": "取り消す決定", "reason": "理由"},
        ])
        retracted_id = result["created"][1]["decision_id"]

        retract("decision", [retracted_id])

        checkin = check_in(aid)
        assert "error" not in checkin
        # coverage分母が1（retracted分を含まない）
        assert checkin["coverage"]["decisions"] == "1/1"


class TestSearchFilter:
    """searchのretractフィルタ"""

    def test_retracted_decision_excluded_from_search(self, topic):
        """retractされたdecisionはsearchでデフォルト除外される"""
        from src.services.search_service import search

        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "検索対象のユニーク決定ABC", "reason": "理由"},
        ])
        retracted_id = result["created"][0]["decision_id"]

        retract("decision", [retracted_id])

        search_result = search("ユニーク決定ABC")
        ids = [(r["type"], r["id_raw"]) for r in search_result.get("results", [])]
        assert ("decision", retracted_id) not in ids

    def test_retracted_log_excluded_from_search(self, topic):
        """retractされたlogはsearchでデフォルト除外される"""
        from src.services.search_service import search

        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "検索対象のユニークログXYZ", "title": "ユニークログXYZ"},
        ])
        retracted_id = result["created"][0]["log_id"]

        retract("log", [retracted_id])

        search_result = search("ユニークログXYZ")
        ids = [(r["type"], r["id_raw"]) for r in search_result.get("results", [])]
        assert ("log", retracted_id) not in ids

    def test_retracted_physically_removed_from_search_index(self, topic):
        """retract時にsearch_index/search_index_fts/vec_indexから物理削除される"""
        from src.services.search_service import search

        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "撤回テスト用ユニーク決定DEF", "reason": "理由"},
        ])
        retracted_id = result["created"][0]["decision_id"]

        # retract前: search_indexにエントリが存在することを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM search_index WHERE source_type = 'decision' AND source_id = ?",
                (retracted_id,),
            ).fetchone()
            assert row is not None
            search_index_id = row["id"]
        finally:
            conn.close()

        retract("decision", [retracted_id])

        # retract後: search_index / search_index_fts / vec_index 全てから消えている
        conn = get_connection()
        try:
            si_row = conn.execute(
                "SELECT id FROM search_index WHERE source_type = 'decision' AND source_id = ?",
                (retracted_id,),
            ).fetchone()
            assert si_row is None

            vec_row = conn.execute(
                "SELECT rowid FROM vec_index WHERE rowid = ?",
                (search_index_id,),
            ).fetchone()
            assert vec_row is None
        finally:
            conn.close()

        # search経由でもヒットしない（物理削除されているため）
        search_result = search("撤回テスト用ユニーク決定DEF")
        ids = [(r["type"], r["id_raw"]) for r in search_result.get("results", [])]
        assert ("decision", retracted_id) not in ids

    def test_retracted_log_physically_removed_from_search_index(self, topic):
        """logもretract時にsearch_index/FTS/vec_indexから物理削除される"""
        from src.services.search_service import search

        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "物理削除テスト用ユニークログGHI", "title": "物理削除テストGHI"},
        ])
        retracted_id = result["created"][0]["log_id"]

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM search_index WHERE source_type = 'log' AND source_id = ?",
                (retracted_id,),
            ).fetchone()
            assert row is not None
            search_index_id = row["id"]
        finally:
            conn.close()

        retract("log", [retracted_id])

        conn = get_connection()
        try:
            si_row = conn.execute(
                "SELECT id FROM search_index WHERE source_type = 'log' AND source_id = ?",
                (retracted_id,),
            ).fetchone()
            assert si_row is None

            vec_row = conn.execute(
                "SELECT rowid FROM vec_index WHERE rowid = ?",
                (search_index_id,),
            ).fetchone()
            assert vec_row is None
        finally:
            conn.close()

        search_result = search("物理削除テストGHI")
        ids = [(r["type"], r["id_raw"]) for r in search_result.get("results", [])]
        assert ("log", retracted_id) not in ids

    def test_retracted_material_physically_removed_from_search_index(self, temp_db):
        """materialもretract時にsearch_index/FTS/vec_indexから物理削除される"""
        from src.services.material_service import add_material
        from src.services.search_service import search

        m = add_material(
            title="物理削除テストMNO",
            content="物理削除テスト用ユニーク資材MNO 本文",
            tags=DEFAULT_TAGS,
            source="unit test",
        )
        retracted_id = m["material_id"]

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM search_index WHERE source_type = 'material' AND source_id = ?",
                (retracted_id,),
            ).fetchone()
            assert row is not None
            search_index_id = row["id"]
        finally:
            conn.close()

        retract("material", [retracted_id])

        conn = get_connection()
        try:
            si_row = conn.execute(
                "SELECT id FROM search_index WHERE source_type = 'material' AND source_id = ?",
                (retracted_id,),
            ).fetchone()
            assert si_row is None

            vec_row = conn.execute(
                "SELECT rowid FROM vec_index WHERE rowid = ?",
                (search_index_id,),
            ).fetchone()
            assert vec_row is None
        finally:
            conn.close()

        search_result = search("物理削除テストMNO")
        ids = [(r["type"], r["id_raw"]) for r in search_result.get("results", [])]
        assert ("material", retracted_id) not in ids

    def test_undo_reindexes(self, topic):
        """un-retract後、retracted_atがNULLに戻ると同時にsearch_indexへ再登録される"""
        from src.services.search_service import search

        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "アンリトラクトテスト用JKL", "reason": "理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        # retract → un-retract
        retract("decision", [decision_id])
        retract("decision", [decision_id], undo=True)

        # un-retract後: retracted_at は NULL に戻る
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT retracted_at FROM decisions WHERE id = ?",
                (decision_id,),
            ).fetchone()
            assert row["retracted_at"] is None

            # search_indexエントリも再登録される
            si_row = conn.execute(
                "SELECT id FROM search_index WHERE source_type = 'decision' AND source_id = ?",
                (decision_id,),
            ).fetchone()
            assert si_row is not None
        finally:
            conn.close()

        # search経由でもヒットする
        search_result = search("アンリトラクトテストJKL")
        ids = [(r["type"], r["id_raw"]) for r in search_result.get("results", [])]
        assert ("decision", decision_id) in ids


class TestPinnedMaterialFilter:
    """check_in の pinned material retract フィルタ"""

    def test_pinned_retracted_material_excluded_from_checkin(self, activity_with_topic):
        """pinsテーブルでpinされたmaterialをretractすると、check-inのpinnedに含まれない"""
        from src.services.material_service import add_material

        aid = activity_with_topic["activity_id"]
        m = add_material(
            title="pinしてretractする資材",
            content="本文",
            tags=DEFAULT_TAGS,
            source="unit test",
        )
        material_id = m["material_id"]

        add_pin("activity", aid, "material", material_id)
        retract("material", [material_id])

        checkin = check_in(aid)
        assert "error" not in checkin

        pinned = checkin.get("pinned", {})
        pinned_material_ids = [m["id_raw"] for m in pinned.get("materials", [])]
        assert material_id not in pinned_material_ids


class TestTimelineMaterialFilter:
    """get_timeline の material retract フィルタ"""

    def test_retracted_material_excluded_from_timeline(self, activity_with_topic):
        """retracted materialはget_timelineで除外される"""
        from src.services.material_service import add_material
        from src.services.relation_service import add_relation
        from src.services.timeline_service import get_timeline

        tid = activity_with_topic["topic_id"]
        m = add_material(
            title="timeline対象",
            content="本文",
            tags=DEFAULT_TAGS,
            source="unit test",
        )
        material_id = m["material_id"]
        add_relation("material", material_id, [{"type": "topic", "ids": [tid]}])

        retract("material", [material_id])

        result = get_timeline(topic_id=tid, entity_types=["material"])
        ids = [item["id_raw"] for item in result.get("items", [])]
        assert material_id not in ids
