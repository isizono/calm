"""トピック管理API（書き込み系）のテスト"""
import os
import tempfile
import pytest
from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.activity_service import add_activity
from tests.helpers import add_log, add_decision


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


DEFAULT_TAGS = ["domain:test"]


def test_add_topic_success(temp_db):
    """トピックの追加が成功する"""
    result = add_topic(
        title="開発フローの詳細",
        description="プランモードの使い方、タスク分解の粒度を決定する",
        tags=DEFAULT_TAGS,
    )

    assert "error" not in result
    assert result["topic_id"] > 0


def test_add_topic_tags_stored(temp_db):
    """トピック作成時にtopic_tagsにレコードが正しくINSERTされる"""
    result = add_topic(
        title="タグテスト",
        description="タグの永続化テスト",
        tags=["domain:cc-memory", "hooks", "intent:design"],
    )

    assert "error" not in result

    # DBで直接確認
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT t.namespace, t.name
            FROM tags t
            JOIN topic_tags tt ON t.id = tt.tag_id
            WHERE tt.topic_id = ?
            ORDER BY t.namespace, t.name
            """,
            (result["topic_id"],),
        ).fetchall()
        assert len(rows) == 3
    finally:
        conn.close()


def test_add_topic_tags_required(temp_db):
    """tags=[]でTAGS_REQUIREDエラーが返る"""
    result = add_topic(
        title="空タグ",
        description="タグなし",
        tags=[],
    )

    assert "error" in result
    assert result["error"]["code"] == "TAGS_REQUIRED"


def test_add_topic_invalid_namespace(temp_db):
    """不正なnamespaceでINVALID_TAG_NAMESPACEエラーが返る"""
    result = add_topic(
        title="不正NS",
        description="不正なnamespace",
        tags=["invalid:tag"],
    )

    assert "error" in result
    assert result["error"]["code"] == "INVALID_TAG_NAMESPACE"


def test_add_topic_duplicate_tags(temp_db):
    """重複タグは静かに排除される"""
    result = add_topic(
        title="重複タグ",
        description="重複テスト",
        tags=["domain:test", "domain:test", "hooks", "hooks"],
    )

    assert "error" not in result
    assert result["topic_id"] > 0

    # DBで直接確認
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT t.namespace, t.name
            FROM tags t
            JOIN topic_tags tt ON t.id = tt.tag_id
            WHERE tt.topic_id = ?
            ORDER BY t.namespace, t.name
            """,
            (result["topic_id"],),
        ).fetchall()
        tag_names = sorted(f"{r['namespace']}:{r['name']}" if r['namespace'] else r['name'] for r in rows)
        assert tag_names == ["domain:test", "hooks"]
    finally:
        conn.close()


def test_add_topic_empty_string_tags(temp_db):
    """空文字タグはスキップされる"""
    result = add_topic(
        title="空文字タグ",
        description="空文字テスト",
        tags=["domain:test", "", "  ", "hooks"],
    )

    assert "error" not in result
    assert result["topic_id"] > 0

    # DBで直接確認
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT t.namespace, t.name
            FROM tags t
            JOIN topic_tags tt ON t.id = tt.tag_id
            WHERE tt.topic_id = ?
            ORDER BY t.namespace, t.name
            """,
            (result["topic_id"],),
        ).fetchall()
        tag_names = sorted(f"{r['namespace']}:{r['name']}" if r['namespace'] else r['name'] for r in rows)
        assert tag_names == ["domain:test", "hooks"]
    finally:
        conn.close()


def test_add_topic_empty_string_tags_only(temp_db):
    """空文字タグのみの場合、TAGS_REQUIREDエラーになる"""
    result = add_topic(
        title="空文字のみ",
        description="全部空文字",
        tags=["", "  "],
    )

    assert "error" in result
    assert result["error"]["code"] == "TAGS_REQUIRED"


def test_add_log_success(temp_db):
    """議論ログの追加が成功する"""
    # トピックを作成
    topic = add_topic(title="テストトピック", description="Test description", tags=DEFAULT_TAGS)

    # ログを追加
    result = add_log(
        topic_id=topic["topic_id"],
        title="プランモードの議論",
        content="AI: プランモードは設計議論フェーズでは不要だと考えます\nユーザー：同意します。",
    )

    assert "error" not in result
    assert result["log_id"] > 0
    assert result["topic_id"] == topic["topic_id"]
    assert result["title"] == "プランモードの議論"
    assert "content" not in result
    assert "tags" in result
    # tagsはtopicから継承
    assert "domain:test" in result["tags"]
    assert "created_at" in result


def test_add_log_with_tags(temp_db):
    """議論ログに追加タグを指定できる"""
    topic = add_topic(title="テストトピック", description="Test description", tags=["domain:test"])

    result = add_log(
        topic_id=topic["topic_id"],
        title="追加タグテスト",
        content="タグ追加のテスト",
        tags=["extra-tag"],
    )

    assert "error" not in result
    # topic_tags UNION log_tags
    assert "domain:test" in result["tags"]
    assert "extra-tag" in result["tags"]


def test_add_log_without_tags(temp_db):
    """tags=NoneでtopicのタグのみがUNIONされる"""
    topic = add_topic(title="テストトピック", description="Test description", tags=["domain:test", "hooks"])

    result = add_log(
        topic_id=topic["topic_id"],
        title="タグなし",
        content="タグを指定しない",
    )

    assert "error" not in result
    # topicの継承タグが効いている
    assert "domain:test" in result["tags"]
    assert "hooks" in result["tags"]


def test_add_log_multiple(temp_db):
    """同じトピックに複数のログを追加できる"""
    topic = add_topic(title="テストトピック", description="Test description", tags=DEFAULT_TAGS)

    # 3つのログを追加
    log1 = add_log(topic_id=topic["topic_id"], title="タイトル1", content="ログ1")
    log2 = add_log(topic_id=topic["topic_id"], title="タイトル2", content="ログ2")
    log3 = add_log(topic_id=topic["topic_id"], title="タイトル3", content="ログ3")

    assert "error" not in log1
    assert "error" not in log2
    assert "error" not in log3
    assert log1["log_id"] != log2["log_id"] != log3["log_id"]


def test_add_log_invalid_topic(temp_db):
    """存在しないトピックIDでエラーになる"""
    result = add_log(topic_id=99999, title="test title", content="test")

    assert "error" in result
    assert result["error"]["code"] == "CONSTRAINT_VIOLATION"


def test_add_log_title_none_auto_generates(temp_db):
    """title=Noneでcontentの先頭行からtitleが自動生成される"""
    topic = add_topic(title="テストトピック", description="Test description", tags=DEFAULT_TAGS)

    result = add_log(
        topic_id=topic["topic_id"],
        title=None,
        content="自動生成されるタイトル\nこれは本文の2行目です",
    )

    assert "error" not in result
    assert result["title"] == "自動生成されるタイトル"
    assert result["log_id"] > 0


def test_add_log_title_empty_auto_generates(temp_db):
    """title=""でcontentの先頭行からtitleが自動生成される"""
    topic = add_topic(title="テストトピック", description="Test description", tags=DEFAULT_TAGS)

    result = add_log(
        topic_id=topic["topic_id"],
        title="",
        content="空文字titleの自動生成テスト",
    )

    assert "error" not in result
    assert result["title"] == "空文字titleの自動生成テスト"


def test_add_log_title_auto_truncate_at_50(temp_db):
    """contentの最初の行が50文字を超える場合に50文字で切り詰められる"""
    topic = add_topic(title="テストトピック", description="Test description", tags=DEFAULT_TAGS)

    long_first_line = "あ" * 60  # 60文字
    result = add_log(
        topic_id=topic["topic_id"],
        content=long_first_line + "\n2行目",
    )

    assert "error" not in result
    assert result["title"] == "あ" * 50
    assert len(result["title"]) == 50


def test_add_log_title_auto_uses_first_line_only(temp_db):
    """contentが改行を含む場合に最初の行だけがtitleになる"""
    topic = add_topic(title="テストトピック", description="Test description", tags=DEFAULT_TAGS)

    result = add_log(
        topic_id=topic["topic_id"],
        content="1行目のタイトル\n2行目の内容\n3行目の内容",
    )

    assert "error" not in result
    assert result["title"] == "1行目のタイトル"


def test_add_log_title_auto_splits_on_literal_backslash_n(temp_db):
    """contentにリテラルな\\nが含まれる場合もそこでtitleを分割する"""
    topic = add_topic(title="テストトピック", description="Test description", tags=DEFAULT_TAGS)

    result = add_log(
        topic_id=topic["topic_id"],
        content="1行目のタイトル\\n2行目の内容",
    )

    assert "error" not in result
    assert result["title"] == "1行目のタイトル"


def test_add_log_title_none_content_empty_error(temp_db):
    """title=Noneかつcontent=""の場合にエラーが返る"""
    topic = add_topic(title="テストトピック", description="Test description", tags=DEFAULT_TAGS)

    result = add_log(
        topic_id=topic["topic_id"],
        title=None,
        content="",
    )

    assert "error" in result
    assert result["error"]["code"] == "ITEM_ERROR"
    assert "title and content cannot both be empty" in result["error"]["message"]


def test_add_log_explicit_title_unchanged(temp_db):
    """title指定時は従来通り動作する（自動生成されない）"""
    topic = add_topic(title="テストトピック", description="Test description", tags=DEFAULT_TAGS)

    result = add_log(
        topic_id=topic["topic_id"],
        title="明示的なタイトル",
        content="contentの先頭行とは異なる",
    )

    assert "error" not in result
    assert result["title"] == "明示的なタイトル"


def test_add_decision_success(temp_db):
    """決定事項の追加が成功する"""
    # トピックを作成
    topic = add_topic(title="テストトピック", description="Test description", tags=DEFAULT_TAGS)

    # 決定事項を追加
    result = add_decision(
        topic_id=topic["topic_id"],
        decision="設計議論フェーズではプランモード不要。",
        reason="設計議論では自由に発散→収束させたい。",
    )

    assert "error" not in result
    assert result["decision_id"] > 0
    assert "decision" not in result
    assert "reason" not in result
    assert "topic_id" not in result
    assert "tags" not in result


def test_add_decision_with_tags(temp_db):
    """決定事項に追加タグを指定できる"""
    topic = add_topic(title="テストトピック", description="Test description", tags=["domain:test"])

    result = add_decision(
        topic_id=topic["topic_id"],
        decision="タグ付き決定",
        reason="追加タグのテスト",
        tags=["extra"],
    )

    assert "error" not in result
    assert result["decision_id"] > 0
    assert "tags" not in result


def test_add_decision_without_tags(temp_db):
    """tags=NoneでtopicのタグのみがUNIONされる"""
    topic = add_topic(title="テストトピック", description="Test description", tags=["domain:test", "intent:design"])

    result = add_decision(
        topic_id=topic["topic_id"],
        decision="タグなし決定",
        reason="タグを指定しない",
    )

    assert "error" not in result
    assert result["decision_id"] > 0
    assert "tags" not in result


def test_add_decision_without_topic(temp_db):
    """topic_id=Noneでもdecisionは作成できる（relations.belongs_toが登録されない）"""
    result = add_decision(
        decision="グローバルな決定事項",
        reason="サブジェクト全体に関わる",
        topic_id=None,
    )

    assert "error" not in result
    assert "decision_id" in result
    # relations.belongs_to が登録されていないことを確認
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_type='decision' AND source_id=? "
            "AND target_type='topic' AND relation_type='belongs_to'",
            (result["decision_id"],),
        ).fetchone()
        assert row[0] == 0
    finally:
        conn.close()


def test_add_decision_multiple(temp_db):
    """複数の決定事項を追加できる"""
    topic = add_topic(title="テストトピック", description="Test description", tags=DEFAULT_TAGS)

    dec1 = add_decision(
        topic_id=topic["topic_id"],
        decision="決定1",
        reason="理由1",
    )
    dec2 = add_decision(
        topic_id=topic["topic_id"],
        decision="決定2",
        reason="理由2",
    )

    assert "error" not in dec1
    assert "error" not in dec2
    assert dec1["decision_id"] != dec2["decision_id"]


# ========================================
# ON DELETE CASCADE のテスト
# ========================================


def _delete_topic(topic_id: int) -> None:
    """テスト用: トピックを直接SQLで削除するヘルパー"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM discussion_topics WHERE id = ?", (topic_id,))
        conn.commit()
    finally:
        conn.close()


def _count_decisions(topic_id: int) -> int:
    """テスト用: 指定トピックのdecisions件数を返すヘルパー (relations.belongs_to 経由)"""
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT COUNT(*) FROM decisions d
            JOIN relations r ON r.source_type='decision' AND r.source_id=d.id
                            AND r.target_type='topic' AND r.relation_type='belongs_to'
                            AND r.target_id = ?
            """,
            (topic_id,),
        )
        return cursor.fetchone()[0]
    finally:
        conn.close()


def _count_logs(topic_id: int) -> int:
    """テスト用: 指定トピックのdiscussion_logs件数を返すヘルパー (relations.belongs_to 経由)"""
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT COUNT(*) FROM discussion_logs l
            JOIN relations r ON r.source_type='log' AND r.source_id=l.id
                            AND r.target_type='topic' AND r.relation_type='belongs_to'
                            AND r.target_id = ?
            """,
            (topic_id,),
        )
        return cursor.fetchone()[0]
    finally:
        conn.close()


def test_on_delete_cascade_decisions(temp_db):
    """トピック削除時にdecisionsがカスケード削除される"""
    topic = add_topic(
        title="カスケードテストトピック",
        description="ON DELETE CASCADEの動作確認",
        tags=DEFAULT_TAGS,
    )
    topic_id = topic["topic_id"]

    # decisionsを2件追加
    add_decision(
        topic_id=topic_id,
        decision="カスケードテスト決定1",
        reason="テスト理由1",
    )
    add_decision(
        topic_id=topic_id,
        decision="カスケードテスト決定2",
        reason="テスト理由2",
    )

    # 削除前に2件あることを確認
    assert _count_decisions(topic_id) == 2

    # トピックを削除
    _delete_topic(topic_id)

    # decisionsがカスケード削除されて0件になることを確認
    assert _count_decisions(topic_id) == 0


def test_on_delete_cascade_discussion_logs(temp_db):
    """トピック削除時にdiscussion_logsがカスケード削除される"""
    topic = add_topic(
        title="ログカスケードテストトピック",
        description="discussion_logsのON DELETE CASCADE確認",
        tags=DEFAULT_TAGS,
    )
    topic_id = topic["topic_id"]

    # discussion_logsを3件追加
    add_log(topic_id=topic_id, title="カスケード1", content="ログ1: カスケードテスト")
    add_log(topic_id=topic_id, title="カスケード2", content="ログ2: カスケードテスト")
    add_log(topic_id=topic_id, title="カスケード3", content="ログ3: カスケードテスト")

    # 削除前に3件あることを確認
    assert _count_logs(topic_id) == 3

    # トピックを削除
    _delete_topic(topic_id)

    # discussion_logsがカスケード削除されて0件になることを確認
    assert _count_logs(topic_id) == 0


# ========================================
# add_topic の related 引数テスト
# ========================================


def test_add_topic_with_related_topic(temp_db):
    """related指定でトピック作成時にトピック間リレーションが張られる"""
    # 先にリレーション先のトピックを作成
    t1 = add_topic(title="関連トピック", description="関連先", tags=DEFAULT_TAGS)
    assert "error" not in t1

    # related付きでトピックを作成
    t2 = add_topic(
        title="新しいトピック",
        description="related付き",
        tags=DEFAULT_TAGS,
        related=[{"type": "topic", "ids": [t1["topic_id"]]}],
    )

    assert "error" not in t2
    assert t2["topic_id"] > 0

    # relationsテーブルに正規化されて格納されていることを確認
    conn = get_connection()
    try:
        id_1 = min(t1["topic_id"], t2["topic_id"])
        id_2 = max(t1["topic_id"], t2["topic_id"])
        row = conn.execute(
            "SELECT * FROM relations WHERE source_type = 'topic' AND source_id = ? AND target_type = 'topic' AND target_id = ?",
            (id_1, id_2),
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_add_topic_with_related_activity(temp_db):
    """related指定でトピック作成時にトピック-アクティビティ間リレーションが張られる"""
    # アクティビティを先に作成
    activity = add_activity(
        title="テストアクティビティ",
        description="テスト用",
        tags=DEFAULT_TAGS,
        check_in=False,
    )
    assert "error" not in activity

    # related付きでトピックを作成
    t = add_topic(
        title="アクティビティ関連トピック",
        description="related付き",
        tags=DEFAULT_TAGS,
        related=[{"type": "activity", "ids": [activity["activity_id"]]}],
    )

    assert "error" not in t
    assert t["topic_id"] > 0

    # relationsテーブルに正規化されて格納されていることを確認
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM relations WHERE source_type = 'activity' AND source_id = ? AND target_type = 'topic' AND target_id = ?",
            (activity["activity_id"], t["topic_id"]),
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_add_topic_related_missing_ids_key(temp_db):
    """related内にidsキーが欠落している場合にVALIDATION_ERRORが返る"""
    result = add_topic(
        title="idsキー欠落",
        description="テスト",
        tags=DEFAULT_TAGS,
        related=[{"type": "topic"}],
    )
    assert "error" in result
    assert result["error"]["code"] == "VALIDATION_ERROR"


def test_add_topic_related_missing_type_key(temp_db):
    """related内にtypeキーが欠落している場合にVALIDATION_ERRORが返る"""
    result = add_topic(
        title="typeキー欠落",
        description="テスト",
        tags=DEFAULT_TAGS,
        related=[{"ids": [1]}],
    )
    assert "error" in result
    assert result["error"]["code"] == "VALIDATION_ERROR"


def test_add_topic_related_invalid_type(temp_db):
    """related内のtypeが不正な場合にINVALID_ENTITY_TYPEが返る"""
    result = add_topic(
        title="不正type",
        description="テスト",
        tags=DEFAULT_TAGS,
        related=[{"type": "invalid", "ids": [1]}],
    )
    assert "error" in result
    assert result["error"]["code"] == "INVALID_ENTITY_TYPE"


def test_add_topic_related_empty_ids(temp_db):
    """related内のidsが空リストの場合にVALIDATION_ERRORが返る"""
    result = add_topic(
        title="空ids",
        description="テスト",
        tags=DEFAULT_TAGS,
        related=[{"type": "topic", "ids": []}],
    )
    assert "error" in result
    assert result["error"]["code"] == "VALIDATION_ERROR"


def test_add_topic_related_empty_list(temp_db):
    """related=[]の場合はリレーションなしでトピックが正常に作成される"""
    result = add_topic(
        title="空リスト",
        description="related空リスト",
        tags=DEFAULT_TAGS,
        related=[],
    )
    assert "error" not in result
    assert result["topic_id"] > 0


def test_add_topic_without_related(temp_db):
    """related未指定でリレーションなしのトピックが作成される"""
    t = add_topic(
        title="リレーションなし",
        description="related未指定",
        tags=DEFAULT_TAGS,
    )

    assert "error" not in t

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM relations WHERE (source_type = 'topic' AND source_id = ?) OR (target_type = 'topic' AND target_id = ?)",
            (t["topic_id"], t["topic_id"]),
        ).fetchone()
        assert row["cnt"] == 0
    finally:
        conn.close()


