"""timeline_service のテスト

トピックまたはアクティビティに紐づくdecision・log・materialを
時系列で混合取得するget_timeline関数をカバーする。
"""
import os
import tempfile

import pytest

from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.discussion_log_service import add_logs
from src.services.decision_service import add_decisions
from src.services.material_service import add_material
from src.services.activity_service import add_activity
from src.services.relation_service import add_relation
from src.services.retract_service import retract
from src.services.timeline_service import get_timeline
from src.services.tag_service import _injected_tags


DEFAULT_TAGS = ["domain:test"]


def _insert_log(conn, topic_id, content, title, created_at=None):
    """discussion_logsに直接INSERTし、relations.belongs_toも併せて作成するヘルパー。"""
    if created_at is not None:
        cur = conn.execute(
            "INSERT INTO discussion_logs (content, title, created_at) VALUES (?, ?, ?)",
            (content, title, created_at),
        )
    else:
        cur = conn.execute(
            "INSERT INTO discussion_logs (content, title) VALUES (?, ?)",
            (content, title),
        )
    log_id = cur.lastrowid
    conn.execute(
        "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) "
        "VALUES ('log', ?, 'topic', ?, 'belongs_to')",
        (log_id, topic_id),
    )
    return log_id


def _insert_decision(conn, topic_id, decision, reason, created_at=None, decision_id=None):
    """decisionsに直接INSERTし、relations.belongs_toも併せて作成するヘルパー。"""
    if decision_id is not None and created_at is not None:
        cur = conn.execute(
            "INSERT INTO decisions (id, decision, reason, created_at) VALUES (?, ?, ?, ?)",
            (decision_id, decision, reason, created_at),
        )
    elif decision_id is not None:
        cur = conn.execute(
            "INSERT INTO decisions (id, decision, reason) VALUES (?, ?, ?)",
            (decision_id, decision, reason),
        )
    elif created_at is not None:
        cur = conn.execute(
            "INSERT INTO decisions (decision, reason, created_at) VALUES (?, ?, ?)",
            (decision, reason, created_at),
        )
    else:
        cur = conn.execute(
            "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
            (decision, reason),
        )
    did = decision_id if decision_id is not None else cur.lastrowid
    conn.execute(
        "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) "
        "VALUES ('decision', ?, 'topic', ?, 'belongs_to')",
        (did, topic_id),
    )
    return did


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
def topic_with_data(topic):
    """decision, log, materialが紐づいたトピックを作成する"""
    tid = topic["topic_id"]

    # decision追加
    dec_result = add_decisions([
        {"topic_id": tid, "decision": "テスト決定1", "reason": "理由1"},
    ])

    # log追加
    log_result = add_logs([
        {"topic_id": tid, "content": "ログ内容1", "title": "テストログ1"},
    ])

    # material追加（topic関連付き）
    mat_result = add_material(
        title="テスト資材1",
        content="資材の内容",
        tags=DEFAULT_TAGS,
        source="テスト用データ",
        related=[{"type": "topic", "ids": [tid]}],
    )

    return {
        "topic_id": tid,
        "decision_id": dec_result["created"][0]["decision_id"],
        "log_id": log_result["created"][0]["log_id"],
        "material_id": mat_result["material_id"],
    }


class TestGetTimelineValidation:
    """バリデーションエラー"""

    def test_both_topic_and_activity_id_raises_error(self, temp_db):
        """topic_idとactivity_idの両方を指定するとバリデーションエラーになる"""
        result = get_timeline(topic_id=1, activity_id=1)
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "mutually exclusive" in result["error"]["message"]

    def test_neither_topic_nor_activity_id_raises_error(self, temp_db):
        """topic_idとactivity_idのどちらも指定しないとバリデーションエラーになる"""
        result = get_timeline()
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "required" in result["error"]["message"]

    def test_invalid_entity_type(self, temp_db):
        """無効なentity_typesを指定するとバリデーションエラーになる"""
        result = get_timeline(topic_id=1, entity_types=["invalid"])
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "Invalid entity_types" in result["error"]["message"]

    def test_empty_entity_types(self, temp_db):
        """空のentity_typesを指定するとバリデーションエラーになる"""
        result = get_timeline(topic_id=1, entity_types=[])
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_order(self, temp_db):
        """無効なorderを指定するとバリデーションエラーになる"""
        result = get_timeline(topic_id=1, order="random")
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "Invalid order" in result["error"]["message"]

    def test_partial_invalid_entity_types(self, temp_db):
        """一部が無効なentity_typesを指定するとバリデーションエラーになる"""
        result = get_timeline(topic_id=1, entity_types=["decision", "invalid"])
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_before_format(self, temp_db):
        """不正なbefore文字列を指定するとバリデーションエラーになる"""
        result = get_timeline(topic_id=1, before="not-a-date")
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "Invalid before" in result["error"]["message"]

    def test_valid_before_format_accepted(self, topic):
        """正しいISO 8601形式のbeforeは受け付けられる"""
        result = get_timeline(topic_id=topic["topic_id"], before="2025-01-01 00:00:00")
        assert "error" not in result

    def test_valid_before_date_only_accepted(self, topic):
        """日付のみのISO 8601形式も受け付けられる"""
        result = get_timeline(topic_id=topic["topic_id"], before="2025-01-01")
        assert "error" not in result


class TestGetTimelineByTopicId:
    """topic_id指定でのタイムライン取得"""

    def test_returns_all_entity_types(self, topic_with_data):
        """topic_id指定でlogs/decisions/materialsが混合で返る"""
        result = get_timeline(topic_id=topic_with_data["topic_id"])
        assert "error" not in result
        assert len(result["items"]) == 3
        assert result["total"] == 3

        types = {item["type"] for item in result["items"]}
        assert types == {"decision", "log", "material"}

    def test_each_item_has_required_fields(self, topic_with_data):
        """各アイテムがid, type, title, created_at, replaces, replaced_byを持つ"""
        result = get_timeline(topic_id=topic_with_data["topic_id"])
        for item in result["items"]:
            assert "id" in item
            assert "type" in item
            assert "title" in item
            assert "created_at" in item
            assert "replaces" in item
            assert "replaced_by" in item

    def test_replaces_and_replaced_by_null_without_supersedes(self, topic_with_data):
        """supersedes関係がないエンティティのreplaces/replaced_byはnull"""
        result = get_timeline(topic_id=topic_with_data["topic_id"])
        for item in result["items"]:
            assert item["replaces"] is None
            assert item["replaced_by"] is None

    def test_nonexistent_topic_returns_empty(self, temp_db):
        """存在しないtopic_idを指定すると空のリストが返る"""
        result = get_timeline(topic_id=99999)
        assert "error" not in result
        assert result["items"] == []
        assert result["total"] == 0

    def test_decision_title_uses_decision_column(self, topic):
        """decisionのtitleはdecisionカラムの値を使う"""
        tid = topic["topic_id"]
        add_decisions([
            {"topic_id": tid, "decision": "決定の内容テキスト", "reason": "理由テキスト"},
        ])

        result = get_timeline(topic_id=tid)
        decision_items = [i for i in result["items"] if i["type"] == "decision"]
        assert len(decision_items) == 1
        assert decision_items[0]["title"] == "決定の内容テキスト"


class TestGetTimelineByActivityId:
    """activity_id指定でのタイムライン取得"""

    def test_aggregates_from_related_topics(self, temp_db):
        """activity_id指定でrelated topicsのエンティティが集約される"""
        # トピック作成
        topic1 = add_topic(title="トピック1", description="テスト", tags=DEFAULT_TAGS)
        topic2 = add_topic(title="トピック2", description="テスト", tags=DEFAULT_TAGS)
        tid1 = topic1["topic_id"]
        tid2 = topic2["topic_id"]

        # アクティビティ作成・リレーション追加
        act = add_activity(
            title="テストアクティビティ",
            description="テスト用",
            tags=DEFAULT_TAGS,
            related=[{"type": "topic", "ids": [tid1, tid2]}],
            check_in=False,
        )
        aid = act["activity_id"]

        # 各トピックにdecisionを追加
        add_decisions([
            {"topic_id": tid1, "decision": "トピック1の決定", "reason": "理由"},
        ])
        add_decisions([
            {"topic_id": tid2, "decision": "トピック2の決定", "reason": "理由"},
        ])

        result = get_timeline(activity_id=aid)
        assert "error" not in result
        assert result["total"] == 2
        titles = {item["title"] for item in result["items"]}
        assert "トピック1の決定" in titles
        assert "トピック2の決定" in titles

    def test_nonexistent_activity_returns_empty(self, temp_db):
        """関連トピックが存在しないactivity_idを指定すると空のリストが返る"""
        result = get_timeline(activity_id=99999)
        assert "error" not in result
        assert result["items"] == []
        assert result["total"] == 0


class TestEntityTypesFilter:
    """entity_typesフィルタ"""

    def test_filter_decision_only(self, topic_with_data):
        """entity_types=["decision"]でdecisionのみ返る"""
        result = get_timeline(
            topic_id=topic_with_data["topic_id"],
            entity_types=["decision"],
        )
        assert "error" not in result
        assert all(item["type"] == "decision" for item in result["items"])
        assert result["total"] == 1

    def test_filter_log_only(self, topic_with_data):
        """entity_types=["log"]でlogのみ返る"""
        result = get_timeline(
            topic_id=topic_with_data["topic_id"],
            entity_types=["log"],
        )
        assert "error" not in result
        assert all(item["type"] == "log" for item in result["items"])
        assert result["total"] == 1

    def test_filter_material_only(self, topic_with_data):
        """entity_types=["material"]でmaterialのみ返る"""
        result = get_timeline(
            topic_id=topic_with_data["topic_id"],
            entity_types=["material"],
        )
        assert "error" not in result
        assert all(item["type"] == "material" for item in result["items"])
        assert result["total"] == 1

    def test_filter_multiple_types(self, topic_with_data):
        """entity_types=["decision","log"]で2種のみ返る"""
        result = get_timeline(
            topic_id=topic_with_data["topic_id"],
            entity_types=["decision", "log"],
        )
        assert "error" not in result
        types = {item["type"] for item in result["items"]}
        assert types == {"decision", "log"}
        assert result["total"] == 2

    def test_no_filter_returns_all(self, topic_with_data):
        """entity_types未指定で全型が返る"""
        result = get_timeline(topic_id=topic_with_data["topic_id"])
        assert "error" not in result
        types = {item["type"] for item in result["items"]}
        assert types == {"decision", "log", "material"}


class TestPagination:
    """beforeカーソルでのページネーション"""

    def test_before_cursor_filters_older_items(self, topic):
        """beforeで指定した日時より前のアイテムのみ返る"""
        tid = topic["topic_id"]

        # 異なるcreated_atを持つデータを作成
        conn = get_connection()
        try:
            _insert_log(conn, tid, "古いログ", "古いログ", "2025-01-01 00:00:00")
            _insert_log(conn, tid, "新しいログ", "新しいログ", "2025-06-01 00:00:00")
            conn.commit()
        finally:
            conn.close()

        # beforeで新しいログの日時を指定 → 古いログのみ返る
        result = get_timeline(topic_id=tid, before="2025-06-01 00:00:00")
        assert "error" not in result
        assert len(result["items"]) == 1
        assert result["items"][0]["title"] == "古いログ"

    def test_before_cursor_total_reflects_all_count(self, topic):
        """beforeカーソル使用時もtotalはbefore条件なしの全件数を返す"""
        tid = topic["topic_id"]

        conn = get_connection()
        try:
            _insert_log(conn, tid, "ログ1", "ログ1", "2025-01-01 00:00:00")
            _insert_log(conn, tid, "ログ2", "ログ2", "2025-03-01 00:00:00")
            _insert_log(conn, tid, "ログ3", "ログ3", "2025-06-01 00:00:00")
            conn.commit()
        finally:
            conn.close()

        result = get_timeline(topic_id=tid, before="2025-06-01 00:00:00")
        assert len(result["items"]) == 2  # ログ1 + ログ2
        assert result["total"] == 3  # 全3件

    def test_limit_restricts_results(self, topic):
        """limitで取得件数を制限できる"""
        tid = topic["topic_id"]

        conn = get_connection()
        try:
            for i in range(5):
                _insert_log(conn, tid, f"ログ{i}", f"ログ{i}", f"2025-01-0{i+1} 00:00:00")
            conn.commit()
        finally:
            conn.close()

        result = get_timeline(topic_id=tid, limit=2)
        assert len(result["items"]) == 2
        assert result["total"] == 5


class TestSortOrder:
    """ソート方向"""

    def test_desc_order(self, topic):
        """order=descで新しい順に返る"""
        tid = topic["topic_id"]

        conn = get_connection()
        try:
            _insert_log(conn, tid, "古い", "古いログ", "2025-01-01 00:00:00")
            _insert_log(conn, tid, "新しい", "新しいログ", "2025-06-01 00:00:00")
            conn.commit()
        finally:
            conn.close()

        result = get_timeline(topic_id=tid, order="desc")
        assert result["items"][0]["title"] == "新しいログ"
        assert result["items"][1]["title"] == "古いログ"

    def test_asc_order(self, topic):
        """order=ascで古い順に返る"""
        tid = topic["topic_id"]

        conn = get_connection()
        try:
            _insert_log(conn, tid, "古い", "古いログ", "2025-01-01 00:00:00")
            _insert_log(conn, tid, "新しい", "新しいログ", "2025-06-01 00:00:00")
            conn.commit()
        finally:
            conn.close()

        result = get_timeline(topic_id=tid, order="asc")
        assert result["items"][0]["title"] == "古いログ"
        assert result["items"][1]["title"] == "新しいログ"


class TestLimitClamping:
    """limit上限クランプ"""

    def test_limit_over_100_is_clamped(self, topic):
        """limit=200を指定しても100にクランプされる（エラーにならない）"""
        tid = topic["topic_id"]
        result = get_timeline(topic_id=tid, limit=200)
        assert "error" not in result

    def test_limit_zero_is_clamped_to_1(self, topic):
        """limit=0を指定すると1にクランプされる"""
        tid = topic["topic_id"]

        conn = get_connection()
        try:
            _insert_log(conn, tid, "ログ内容", "テストログ")
            conn.commit()
        finally:
            conn.close()

        result = get_timeline(topic_id=tid, limit=0)
        assert "error" not in result
        assert len(result["items"]) == 1


class TestTotalCount:
    """totalフィールドの正確性"""

    def test_total_equals_all_matching_items(self, topic):
        """totalはlimitに関係なく条件合致の全件数を返す"""
        tid = topic["topic_id"]

        conn = get_connection()
        try:
            for i in range(10):
                _insert_log(conn, tid, f"ログ{i}", f"ログ{i}")
            conn.commit()
        finally:
            conn.close()

        result = get_timeline(topic_id=tid, limit=3)
        assert len(result["items"]) == 3
        assert result["total"] == 10

    def test_total_with_entity_type_filter(self, topic_with_data):
        """entity_typesフィルタ適用時もtotalはフィルタ後の件数を返す"""
        result = get_timeline(
            topic_id=topic_with_data["topic_id"],
            entity_types=["decision"],
        )
        assert result["total"] == 1

    def test_total_with_before_cursor(self, topic):
        """before指定時もtotalは全件数を返す"""
        tid = topic["topic_id"]

        conn = get_connection()
        try:
            _insert_log(conn, tid, "ログ1", "ログ1", "2025-01-01 00:00:00")
            _insert_log(conn, tid, "ログ2", "ログ2", "2025-06-01 00:00:00")
            _insert_decision(conn, tid, "決定1", "理由1", "2025-03-01 00:00:00")
            conn.commit()
        finally:
            conn.close()

        # before指定でフィルタ → itemsは2件だがtotalは全3件
        result = get_timeline(topic_id=tid, before="2025-04-01 00:00:00")
        assert len(result["items"]) == 2  # ログ1 + 決定1
        assert result["total"] == 3  # 全3件


class TestRetractedExclusion:
    """retractされたエンティティはタイムラインから除外される"""

    def test_retracted_decision_excluded(self, topic):
        """retractされたdecisionはタイムラインに表示されない"""
        tid = topic["topic_id"]
        dec_result = add_decisions([
            {"topic_id": tid, "decision": "通常の決定", "reason": "理由"},
            {"topic_id": tid, "decision": "取り消す決定", "reason": "理由"},
        ])
        retract_id = dec_result["created"][1]["decision_id"]

        retract(entity_type="decision", ids=[retract_id])

        result = get_timeline(topic_id=tid)
        assert result["total"] == 1
        assert all(item["id"] != retract_id for item in result["items"])

    def test_retracted_log_excluded(self, topic):
        """retractされたlogはタイムラインに表示されない"""
        tid = topic["topic_id"]
        log_result = add_logs([
            {"topic_id": tid, "content": "通常のログ", "title": "通常"},
            {"topic_id": tid, "content": "取り消すログ", "title": "取り消し"},
        ])
        retract_id = log_result["created"][1]["log_id"]

        retract(entity_type="log", ids=[retract_id])

        result = get_timeline(topic_id=tid)
        assert result["total"] == 1
        assert all(item["id"] != retract_id for item in result["items"])


class TestMaterialDedup:
    """activity_id指定時のmaterial重複排除"""

    def test_shared_material_not_duplicated(self, temp_db):
        """複数トピックが共有するmaterialが重複して返らない"""
        topic1 = add_topic(title="トピック1", description="テスト", tags=DEFAULT_TAGS)
        topic2 = add_topic(title="トピック2", description="テスト", tags=DEFAULT_TAGS)
        tid1 = topic1["topic_id"]
        tid2 = topic2["topic_id"]

        act = add_activity(
            title="テストアクティビティ",
            description="テスト用",
            tags=DEFAULT_TAGS,
            related=[{"type": "topic", "ids": [tid1, tid2]}],
            check_in=False,
        )
        aid = act["activity_id"]

        # 1つのmaterialを両方のトピックに紐づける
        mat = add_material(
            title="共有資材",
            content="共有の内容",
            tags=DEFAULT_TAGS,
            source="テスト用データ",
            related=[{"type": "topic", "ids": [tid1, tid2]}],
        )

        result = get_timeline(activity_id=aid, entity_types=["material"])
        material_ids = [item["id"] for item in result["items"]]
        assert len(material_ids) == 1
        assert result["total"] == 1


class TestMixedTimeline:
    """異なるエンティティ型の混合ソート"""

    def test_mixed_types_sorted_by_created_at(self, topic):
        """異なるエンティティ型がcreated_atでソートされる"""
        tid = topic["topic_id"]

        conn = get_connection()
        try:
            _insert_log(conn, tid, "ログ", "ログA", "2025-01-01 00:00:00")
            _insert_decision(conn, tid, "決定B", "理由", "2025-02-01 00:00:00")
            # material: relationsテーブルを直接作成（正規化: 'material' < 'topic'）
            cursor = conn.execute(
                "INSERT INTO materials (title, content, source, created_at) VALUES (?, ?, ?, ?)",
                ("資材C", "内容", "テスト用データ", "2025-03-01 00:00:00"),
            )
            mat_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) VALUES ('material', ?, 'topic', ?, 'belongs_to')",
                (mat_id, tid),
            )
            conn.commit()
        finally:
            conn.close()

        result = get_timeline(topic_id=tid, order="asc")
        assert len(result["items"]) == 3
        assert result["items"][0]["title"] == "ログA"
        assert result["items"][0]["type"] == "log"
        assert result["items"][1]["title"] == "決定B"
        assert result["items"][1]["type"] == "decision"
        assert result["items"][2]["title"] == "資材C"
        assert result["items"][2]["type"] == "material"


class TestSupersedes:
    """decision_supersedesによるreplaces/replaced_byの表示"""

    def test_decision_replaces_shows_target(self, topic):
        """supersedesのsource側decisionのreplacesにtarget情報が入る"""
        tid = topic["topic_id"]

        conn = get_connection()
        try:
            _insert_decision(conn, tid, "旧決定", "理由", decision_id=1001)
            _insert_decision(conn, tid, "新決定", "理由", decision_id=1002)
            conn.execute(
                "INSERT INTO decision_supersedes (source_id, target_id) VALUES (?, ?)",
                (1002, 1001),
            )
            conn.commit()
        finally:
            conn.close()

        result = get_timeline(topic_id=tid, entity_types=["decision"])
        # α化により id は文字列になるので id_raw で索引する
        items_by_id = {item["id_raw"]: item for item in result["items"]}

        # 新決定(source)は旧決定(target)をreplaces（replaces には title が無いので id="(#1001)"）
        replaces = items_by_id[1002]["replaces"]
        assert replaces is not None
        assert replaces["type"] == "decision"
        assert replaces["id_raw"] == 1001
        assert items_by_id[1002]["replaced_by"] is None

    def test_decision_replaced_by_shows_source(self, topic):
        """supersedesのtarget側decisionのreplaced_byにsource情報が入る"""
        tid = topic["topic_id"]

        conn = get_connection()
        try:
            _insert_decision(conn, tid, "旧決定", "理由", decision_id=2001)
            _insert_decision(conn, tid, "新決定", "理由", decision_id=2002)
            conn.execute(
                "INSERT INTO decision_supersedes (source_id, target_id) VALUES (?, ?)",
                (2002, 2001),
            )
            conn.commit()
        finally:
            conn.close()

        result = get_timeline(topic_id=tid, entity_types=["decision"])
        items_by_id = {item["id_raw"]: item for item in result["items"]}

        # 旧決定(target)は新決定(source)にreplaced_by
        replaced_by = items_by_id[2001]["replaced_by"]
        assert replaced_by is not None
        assert replaced_by["type"] == "decision"
        assert replaced_by["id_raw"] == 2002
        assert items_by_id[2001]["replaces"] is None

    def test_log_and_material_replaces_null_even_with_supersedes(self, topic):
        """supersedes関係を持つdecisionが存在してもlog/materialのreplaces/replaced_byはnull"""
        tid = topic["topic_id"]

        # supersedes関係のあるdecisionを作成
        conn = get_connection()
        try:
            _insert_decision(conn, tid, "旧決定", "理由", decision_id=3001)
            _insert_decision(conn, tid, "新決定", "理由", decision_id=3002)
            conn.execute(
                "INSERT INTO decision_supersedes (source_id, target_id) VALUES (?, ?)",
                (3002, 3001),
            )
            conn.commit()
        finally:
            conn.close()

        add_logs([{"topic_id": tid, "content": "テストログ", "title": "ログ"}])
        add_material(
            title="テスト資材",
            content="内容",
            tags=DEFAULT_TAGS,
            source="テスト用データ",
            related=[{"type": "topic", "ids": [tid]}],
        )

        result = get_timeline(topic_id=tid)
        non_decision_items = [i for i in result["items"] if i["type"] != "decision"]
        assert len(non_decision_items) == 2
        for item in non_decision_items:
            assert item["replaces"] is None
            assert item["replaced_by"] is None
