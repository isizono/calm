"""トピック管理API（読み取り系）のテスト

get_topicsはtags引数でフィルタリングする。
get_logs/get_decisionsは各アイテムにtagsフィールドを含む。
"""
import os
import tempfile
import pytest
from src.db import init_database, get_connection
from src.services.topic_service import (
    add_topic,
    get_topics,
    TOPIC_DESC_MAX_LEN,
)
from tests.helpers import add_log, add_decision
from src.services.discussion_log_service import get_logs
from src.services.decision_service import get_decisions


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        # クリーンアップ
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


# ========================================
# get-topics のテスト
# ========================================


def test_get_topics_empty(temp_db):
    """tags指定だがマッチするtopicが0件"""
    result = get_topics(tags=["domain:nonexistent"])

    assert "error" not in result
    assert result["topics"] == []
    assert result["total_count"] == 0


def test_get_topics_desc_order(temp_db):
    """複数topic作成、降順確認"""
    add_topic(title="Topic A", description="First", tags=DEFAULT_TAGS)
    add_topic(title="Topic B", description="Second", tags=DEFAULT_TAGS)
    add_topic(title="Topic C", description="Third", tags=DEFAULT_TAGS)

    result = get_topics(tags=DEFAULT_TAGS)

    assert "error" not in result
    # init_databaseで作成されるfirst_topicは"domain:default"なのでDEFAULT_TAGSには含まれない
    assert len(result["topics"]) == 3
    # 降順（新しい順）
    assert result["topics"][0]["title"] == "Topic C"
    assert result["topics"][1]["title"] == "Topic B"
    assert result["topics"][2]["title"] == "Topic A"


def test_get_topics_pagination(temp_db):
    """offset/limit動作確認"""
    for i in range(5):
        add_topic(title=f"Topic {i}", description=f"Desc {i}", tags=DEFAULT_TAGS)

    result = get_topics(tags=DEFAULT_TAGS, limit=2, offset=0)

    assert "error" not in result
    assert len(result["topics"]) == 2
    assert result["total_count"] == 5

    result2 = get_topics(tags=DEFAULT_TAGS, limit=2, offset=2)
    assert len(result2["topics"]) == 2

    result3 = get_topics(tags=DEFAULT_TAGS, limit=2, offset=4)
    assert len(result3["topics"]) == 1


def test_get_topics_offset_beyond_total(temp_db):
    """offset超過で空配列"""
    add_topic(title="Only One", description="Desc", tags=DEFAULT_TAGS)

    result = get_topics(tags=DEFAULT_TAGS, offset=100)

    assert "error" not in result
    assert result["topics"] == []
    assert result["total_count"] == 1


def test_get_topics_invalid_limit(temp_db):
    """limit=0でINVALID_PARAMETERエラー"""
    result = get_topics(tags=DEFAULT_TAGS, limit=0)

    assert "error" in result
    assert result["error"]["code"] == "INVALID_PARAMETER"


def test_get_topics_invalid_offset(temp_db):
    """offset=-1でINVALID_PARAMETERエラー"""
    result = get_topics(tags=DEFAULT_TAGS, offset=-1)

    assert "error" in result
    assert result["error"]["code"] == "INVALID_PARAMETER"


def test_get_topics_no_tags_returns_all(temp_db):
    """tags未指定で全トピックを返す（init_databaseのfirst_topic含む）"""
    add_topic(title="Topic A", description="Desc A", tags=["domain:test"])
    add_topic(title="Topic B", description="Desc B", tags=["domain:other"])

    result = get_topics()

    assert "error" not in result
    # init_databaseで作成されるfirst_topicも含む
    assert result["total_count"] >= 3
    titles = {t["title"] for t in result["topics"]}
    assert "Topic A" in titles
    assert "Topic B" in titles


def test_get_topics_no_tags_with_pagination(temp_db):
    """tags未指定 + ページネーション"""
    for i in range(5):
        add_topic(title=f"Topic {i}", description=f"Desc {i}", tags=["domain:test"])

    result = get_topics(limit=2, offset=0)

    assert "error" not in result
    assert len(result["topics"]) == 2
    # first_topicも含むので6件以上
    assert result["total_count"] >= 6


def test_get_topics_tags_empty_list_error(temp_db):
    """tags=[]でTAGS_REQUIREDエラー"""
    result = get_topics(tags=[])

    assert "error" in result
    assert result["error"]["code"] == "TAGS_REQUIRED"


def test_get_topics_nonexistent_tag(temp_db):
    """存在しないタグで空配列"""
    add_topic(title="Topic", description="Desc", tags=DEFAULT_TAGS)

    result = get_topics(tags=["domain:does-not-exist"])

    assert "error" not in result
    assert result["topics"] == []
    assert result["total_count"] == 0


def test_get_topics_partial_nonexistent_tags(temp_db):
    """存在するタグと存在しないタグの混在で空配列が返る"""
    add_topic(title="Topic", description="Desc", tags=["domain:test"])

    result = get_topics(tags=["domain:test", "domain:nonexistent"])

    assert "error" not in result
    assert result["topics"] == []
    assert result["total_count"] == 0


def test_get_topics_and_filter(temp_db):
    """複数タグAND条件"""
    add_topic(title="Both Tags", description="Desc", tags=["domain:test", "intent:design"])
    add_topic(title="Only domain", description="Desc", tags=["domain:test"])
    add_topic(title="Only intent", description="Desc", tags=["intent:design"])

    result = get_topics(tags=["domain:test", "intent:design"])

    assert "error" not in result
    assert result["total_count"] == 1
    assert result["topics"][0]["title"] == "Both Tags"


def test_get_topics_since_filter(temp_db):
    """since指定でcreated_at以降のトピックのみ返す"""
    t1 = add_topic(title="Old Topic", description="Desc", tags=DEFAULT_TAGS)
    t2 = add_topic(title="New Topic", description="Desc", tags=DEFAULT_TAGS)

    conn = get_connection()
    conn.execute(
        "UPDATE discussion_topics SET created_at = '2026-01-01 00:00:00' WHERE id = ?",
        (t1["topic_id"],),
    )
    conn.execute(
        "UPDATE discussion_topics SET created_at = '2026-03-15 00:00:00' WHERE id = ?",
        (t2["topic_id"],),
    )
    conn.commit()
    conn.close()

    result = get_topics(tags=DEFAULT_TAGS, since="2026-03-01")

    assert "error" not in result
    assert result["total_count"] == 1
    assert result["topics"][0]["title"] == "New Topic"


def test_get_topics_until_filter(temp_db):
    """until指定でcreated_at以前のトピックのみ返す"""
    t1 = add_topic(title="Old Topic", description="Desc", tags=DEFAULT_TAGS)
    t2 = add_topic(title="New Topic", description="Desc", tags=DEFAULT_TAGS)

    conn = get_connection()
    conn.execute(
        "UPDATE discussion_topics SET created_at = '2026-01-01 00:00:00' WHERE id = ?",
        (t1["topic_id"],),
    )
    conn.execute(
        "UPDATE discussion_topics SET created_at = '2026-03-15 00:00:00' WHERE id = ?",
        (t2["topic_id"],),
    )
    conn.commit()
    conn.close()

    result = get_topics(tags=DEFAULT_TAGS, until="2026-02-01")

    assert "error" not in result
    assert result["total_count"] == 1
    assert result["topics"][0]["title"] == "Old Topic"


def test_get_topics_since_and_until_combined(temp_db):
    """since+until指定で範囲内のトピックのみ返す"""
    t1 = add_topic(title="Jan Topic", description="Desc", tags=DEFAULT_TAGS)
    t2 = add_topic(title="Feb Topic", description="Desc", tags=DEFAULT_TAGS)
    t3 = add_topic(title="Mar Topic", description="Desc", tags=DEFAULT_TAGS)

    conn = get_connection()
    conn.execute(
        "UPDATE discussion_topics SET created_at = '2026-01-15 00:00:00' WHERE id = ?",
        (t1["topic_id"],),
    )
    conn.execute(
        "UPDATE discussion_topics SET created_at = '2026-02-15 00:00:00' WHERE id = ?",
        (t2["topic_id"],),
    )
    conn.execute(
        "UPDATE discussion_topics SET created_at = '2026-03-15 00:00:00' WHERE id = ?",
        (t3["topic_id"],),
    )
    conn.commit()
    conn.close()

    result = get_topics(tags=DEFAULT_TAGS, since="2026-02-01", until="2026-02-28")

    assert "error" not in result
    assert result["total_count"] == 1
    assert result["topics"][0]["title"] == "Feb Topic"


def test_get_topics_since_no_match(temp_db):
    """sinceが未来日で0件"""
    add_topic(title="Topic", description="Desc", tags=DEFAULT_TAGS)

    conn = get_connection()
    conn.execute("UPDATE discussion_topics SET created_at = '2026-01-01 00:00:00'")
    conn.commit()
    conn.close()

    result = get_topics(tags=DEFAULT_TAGS, since="2099-01-01")

    assert "error" not in result
    assert result["total_count"] == 0
    assert result["topics"] == []


def test_get_topics_since_without_tags(temp_db):
    """tags未指定 + sinceで全トピックから日付フィルタ"""
    t1 = add_topic(title="Old", description="Desc", tags=["domain:a"])
    t2 = add_topic(title="New", description="Desc", tags=["domain:b"])

    conn = get_connection()
    conn.execute(
        "UPDATE discussion_topics SET created_at = '2026-01-01 00:00:00' WHERE id = ?",
        (t1["topic_id"],),
    )
    conn.execute(
        "UPDATE discussion_topics SET created_at = '2026-03-15 00:00:00' WHERE id = ?",
        (t2["topic_id"],),
    )
    # init_databaseのfirst_topicも古い日付にする
    conn.execute(
        "UPDATE discussion_topics SET created_at = '2025-01-01 00:00:00' WHERE id NOT IN (?, ?)",
        (t1["topic_id"], t2["topic_id"]),
    )
    conn.commit()
    conn.close()

    result = get_topics(since="2026-03-01")

    assert "error" not in result
    assert result["total_count"] == 1
    assert result["topics"][0]["title"] == "New"


def test_get_topics_until_includes_same_day(temp_db):
    """until指定日と同日のレコードが含まれる（境界テスト）"""
    t1 = add_topic(title="Same Day", description="Desc", tags=DEFAULT_TAGS)

    conn = get_connection()
    conn.execute(
        "UPDATE discussion_topics SET created_at = '2026-03-15 12:30:00' WHERE id = ?",
        (t1["topic_id"],),
    )
    conn.commit()
    conn.close()

    result = get_topics(tags=DEFAULT_TAGS, until="2026-03-15")

    assert "error" not in result
    assert result["total_count"] == 1
    assert result["topics"][0]["title"] == "Same Day"


def test_get_topics_invalid_since_format(temp_db):
    """不正なsince形式でINVALID_PARAMETERエラー"""
    result = get_topics(tags=DEFAULT_TAGS, since="not-a-date")

    assert "error" in result
    assert result["error"]["code"] == "INVALID_PARAMETER"


def test_get_topics_invalid_until_format(temp_db):
    """不正なuntil形式でINVALID_PARAMETERエラー"""
    result = get_topics(tags=DEFAULT_TAGS, until="2026/03/15")

    assert "error" in result
    assert result["error"]["code"] == "INVALID_PARAMETER"


def test_get_topics_has_tags_field(temp_db):
    """各topicにtags付き"""
    add_topic(title="Tagged Topic", description="Desc", tags=["domain:test", "intent:design"])

    result = get_topics(tags=["domain:test"])

    assert "error" not in result
    assert len(result["topics"]) == 1
    topic = result["topics"][0]
    assert "tags" in topic
    assert "domain:test" in topic["tags"]
    assert "intent:design" in topic["tags"]
    # 旧フィールドが除去されている
    assert "subject_id" not in topic
    assert "parent_topic_id" not in topic
    assert "ancestors" not in topic


def test_get_topics_description_truncated(temp_db):
    """descriptionが長い場合、TOPIC_DESC_MAX_LENで切り詰められる"""
    long_desc = "A" * 500
    add_topic(title="Long Desc Topic", description=long_desc, tags=DEFAULT_TAGS)

    result = get_topics(tags=DEFAULT_TAGS)

    assert "error" not in result
    topic = result["topics"][0]
    assert len(topic["description"]) == TOPIC_DESC_MAX_LEN
    assert topic["description"] == "A" * TOPIC_DESC_MAX_LEN



# ========================================
# get-logs のテスト
# ========================================


def test_get_logs_empty(temp_db):
    """ログが存在しない場合、空の配列が返る"""
    topic = add_topic(title="Topic", description="Test description", tags=DEFAULT_TAGS)
    result = get_logs("topic", topic["topic_id"])

    assert "error" not in result
    assert result["logs"] == []


def test_get_logs_multiple(temp_db):
    """複数のログを取得できる"""
    topic = add_topic(title="Topic", description="Test description", tags=DEFAULT_TAGS)

    # 3つのログを追加
    log1 = add_log(topic_id=topic["topic_id"], title="Title 1", content="Log 1")
    log2 = add_log(topic_id=topic["topic_id"], title="Title 2", content="Log 2")
    log3 = add_log(topic_id=topic["topic_id"], title="Title 3", content="Log 3")

    result = get_logs("topic", topic["topic_id"])

    assert "error" not in result
    assert len(result["logs"]) == 3
    assert result["logs"][0]["id_raw"] == log1["log_id"]
    assert result["logs"][0]["content"] == "Log 1"
    assert result["logs"][1]["id_raw"] == log2["log_id"]
    assert result["logs"][2]["id_raw"] == log3["log_id"]


def test_get_logs_with_pagination(temp_db):
    """ページネーションで取得できる"""
    topic = add_topic(title="Topic", description="Test description", tags=DEFAULT_TAGS)

    # 5つのログを追加
    logs = []
    for i in range(5):
        log = add_log(topic_id=topic["topic_id"], title=f"Title {i}", content=f"Log {i}")
        logs.append(log)

    # 最初の3件を取得
    result1 = get_logs("topic", topic["topic_id"], limit=3)
    assert len(result1["logs"]) == 3

    # 4件目から取得
    result2 = get_logs(
        "topic",
        topic["topic_id"],
        start_id=logs[3]["log_id"],
        limit=3,
    )
    assert len(result2["logs"]) == 2
    assert result2["logs"][0]["id_raw"] == logs[3]["log_id"]


def test_get_logs_with_tags(temp_db):
    """各logにtags含む（topicタグ継承）"""
    topic = add_topic(title="Topic", description="Test", tags=DEFAULT_TAGS)
    add_log(topic_id=topic["topic_id"], title="Log 1", content="Content 1")

    result = get_logs("topic", topic["topic_id"])

    assert "error" not in result
    assert len(result["logs"]) == 1
    log = result["logs"][0]
    assert "tags" in log
    assert "domain:test" in log["tags"]


# ========================================
# get-decisions のテスト
# ========================================


def test_get_decisions_empty(temp_db):
    """決定事項が存在しない場合、空の配列が返る"""
    topic = add_topic(title="Topic", description="Test description", tags=DEFAULT_TAGS)
    result = get_decisions("topic", topic["topic_id"])

    assert "error" not in result
    assert result["topic_id"] == topic["topic_id"]
    assert result["topic_name"] == "Topic"
    assert result["decisions"] == []


def test_get_decisions_topic_name_included(temp_db):
    """topic_nameがトップレベルに含まれる"""
    topic = add_topic(title="テスト用トピック", description="Test", tags=DEFAULT_TAGS)
    add_decision(topic_id=topic["topic_id"], decision="Dec 1", reason="Reason 1")

    result = get_decisions("topic", topic["topic_id"])

    assert result["topic_id"] == topic["topic_id"]
    assert result["topic_name"] == "テスト用トピック"
    assert len(result["decisions"]) == 1
    assert "topic_id" not in result["decisions"][0]


def test_get_decisions_nonexistent_topic(temp_db):
    """存在しないtopic_idの場合、topic_name=nullで空配列"""
    result = get_decisions("topic", 999999)

    assert "error" not in result
    assert result["topic_id"] == 999999
    assert result["topic_name"] is None
    assert result["decisions"] == []


def test_get_decisions_multiple(temp_db):
    """複数の決定事項を取得できる"""
    topic = add_topic(title="Topic", description="Test description", tags=DEFAULT_TAGS)

    # 3つの決定事項を追加
    dec1 = add_decision(
        topic_id=topic["topic_id"],
        decision="Decision 1",
        reason="Reason 1",
    )
    dec2 = add_decision(
        topic_id=topic["topic_id"],
        decision="Decision 2",
        reason="Reason 2",
    )
    dec3 = add_decision(
        topic_id=topic["topic_id"],
        decision="Decision 3",
        reason="Reason 3",
    )

    result = get_decisions("topic", topic["topic_id"])

    assert "error" not in result
    assert len(result["decisions"]) == 3
    assert result["decisions"][0]["id_raw"] == dec1["decision_id"]
    assert result["decisions"][0]["decision"] == "Decision 1"
    assert result["decisions"][1]["id_raw"] == dec2["decision_id"]
    assert result["decisions"][2]["id_raw"] == dec3["decision_id"]


def test_get_decisions_with_pagination(temp_db):
    """ページネーションで取得できる"""
    topic = add_topic(title="Topic", description="Test description", tags=DEFAULT_TAGS)

    # 5つの決定事項を追加
    decisions = []
    for i in range(5):
        dec = add_decision(
            topic_id=topic["topic_id"],
            decision=f"Decision {i}",
            reason=f"Reason {i}",
        )
        decisions.append(dec)

    # 最初の3件を取得
    result1 = get_decisions("topic", topic["topic_id"], limit=3)
    assert len(result1["decisions"]) == 3

    # 4件目から取得
    result2 = get_decisions(
        "topic",
        topic["topic_id"],
        start_id=decisions[3]["decision_id"],
        limit=3,
    )
    assert len(result2["decisions"]) == 2
    assert result2["decisions"][0]["id_raw"] == decisions[3]["decision_id"]


def test_get_decisions_with_tags(temp_db):
    """各decisionにtags含む（topicタグ継承）"""
    topic = add_topic(title="Topic", description="Test", tags=DEFAULT_TAGS)
    add_decision(topic_id=topic["topic_id"], decision="Dec 1", reason="Reason 1")

    result = get_decisions("topic", topic["topic_id"])

    assert "error" not in result
    assert len(result["decisions"]) == 1
    dec = result["decisions"][0]
    assert "tags" in dec
    assert "domain:test" in dec["tags"]


def test_get_decisions_with_extra_tags(temp_db):
    """decision個別タグ+topic継承"""
    topic = add_topic(title="Topic", description="Test", tags=DEFAULT_TAGS)
    add_decision(
        topic_id=topic["topic_id"],
        decision="Dec with extra tags",
        reason="Reason",
        tags=["intent:design"],
    )

    result = get_decisions("topic", topic["topic_id"])

    assert "error" not in result
    assert len(result["decisions"]) == 1
    dec = result["decisions"][0]
    assert "tags" in dec
    # topicのタグを継承
    assert "domain:test" in dec["tags"]
    # decision個別のタグも含む
    assert "intent:design" in dec["tags"]
