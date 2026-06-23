"""add_logs / add_decisions バッチAPI のテスト

V1-V10 の受け入れ基準をカバーする。
"""
import os
import tempfile
import pytest
import numpy as np
from unittest.mock import patch

from src.db import init_database, get_connection, execute_query
from src.services.topic_service import add_topic
from src.services.discussion_log_service import add_logs
from src.services.decision_service import add_decisions
from src.services.tag_service import _injected_tags
import src.services.embedding_service as emb


EMBEDDING_DIM = 384
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
def topic2(temp_db):
    """テスト用トピック2を作成する"""
    return add_topic(title="テストトピック2", description="テスト用2", tags=["domain:test", "extra"])


@pytest.fixture
def mock_embedding_server(monkeypatch):
    """embedding_serverへのHTTPリクエストをモック化"""
    def mock_encode_batch(texts, prefix):
        embeddings = []
        for text in texts:
            prefix_str = "検索文書: " if prefix == "document" else "検索クエリ: "
            np.random.seed(hash(prefix_str + text) % (2**32))
            embeddings.append(np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist())
        return embeddings

    monkeypatch.setattr(emb, '_encode_batch', mock_encode_batch)
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)
    yield


# ========================================
# V1: 3件一括登録が成功する
# ========================================


class TestV1BatchSuccess:
    """V1: 3件一括登録が成功する"""

    def test_add_logs_batch_3_items(self, topic):
        """add_logsで3件一括登録が成功する"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "ログ1の内容", "title": "タイトル1"},
            {"topic_id": tid, "content": "ログ2の内容", "title": "タイトル2"},
            {"topic_id": tid, "content": "ログ3の内容", "title": "タイトル3"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 3
        assert len(result["errors"]) == 0

        for i, c in enumerate(result["created"]):
            assert c["log_id"] > 0
            assert c["topic_id"] == tid
            assert c["title"] == f"タイトル{i + 1}"
            assert "content" not in c  # レスポンス軽量化
            assert "tags" in c
            assert "created_at" in c

    def test_add_decisions_batch_3_items(self, topic):
        """add_decisionsで3件一括登録が成功する"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "決定1", "reason": "理由1"},
            {"topic_id": tid, "decision": "決定2", "reason": "理由2"},
            {"topic_id": tid, "decision": "決定3", "reason": "理由3"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 3
        assert len(result["errors"]) == 0

        for i, c in enumerate(result["created"]):
            assert c["decision_id"] > 0
            # レスポンス軽量化: decision_idのみ含まれる
            assert "topic_id" not in c
            assert "decision" not in c
            assert "reason" not in c
            assert "tags" not in c
            assert "created_at" not in c


# ========================================
# V2: 1件の配列ラップで正常動作する
# ========================================


class TestV2SingleItem:
    """V2: 1件の配列ラップで正常動作する"""

    def test_add_logs_single_item(self, topic):
        """1件のログを配列ラップで登録"""
        result = add_logs([
            {"topic_id": topic["topic_id"], "content": "単件ログ", "title": "単件タイトル"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        assert len(result["errors"]) == 0
        assert result["created"][0]["title"] == "単件タイトル"

    def test_add_decisions_single_item(self, topic):
        """1件の決定事項を配列ラップで登録"""
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "単件決定", "reason": "単件理由"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        assert len(result["errors"]) == 0
        assert result["created"][0]["decision_id"] > 0


# ========================================
# V3: 存在しないtopic_id混在で部分成功する
# ========================================


class TestV3PartialSuccessInvalidTopic:
    """V3: 存在しないtopic_id混在で部分成功する"""

    def test_add_logs_partial_success_invalid_topic(self, topic):
        """存在しないtopic_idを含むバッチで部分成功"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "成功するログ", "title": "成功"},
            {"topic_id": 99999, "content": "失敗するログ", "title": "失敗"},
            {"topic_id": tid, "content": "もう一つ成功", "title": "成功2"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 2
        assert len(result["errors"]) == 1
        assert result["errors"][0]["index"] == 1
        assert result["created"][0]["title"] == "成功"
        assert result["created"][1]["title"] == "成功2"

    def test_add_decisions_partial_success_invalid_topic(self, topic):
        """存在しないtopic_idを含むバッチで部分成功"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "成功する決定", "reason": "理由"},
            {"topic_id": 99999, "decision": "失敗する決定", "reason": "理由"},
            {"topic_id": tid, "decision": "もう一つ成功", "reason": "理由"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 2
        assert len(result["errors"]) == 1
        assert result["errors"][0]["index"] == 1


# ========================================
# V4: 不正tags混在で部分成功する
# ========================================


class TestV4PartialSuccessInvalidTags:
    """V4: 不正tags混在で部分成功する"""

    def test_add_logs_partial_success_invalid_tags(self, topic):
        """不正なタグを含むバッチで部分成功"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "成功するログ", "title": "成功", "tags": ["domain:test"]},
            {"topic_id": tid, "content": "失敗するログ", "title": "失敗", "tags": ["bad:namespace"]},
            {"topic_id": tid, "content": "もう一つ成功", "title": "成功2"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 2
        assert len(result["errors"]) == 1
        assert result["errors"][0]["index"] == 1

    def test_add_decisions_partial_success_invalid_tags(self, topic):
        """不正なタグを含むバッチで部分成功"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "成功", "reason": "理由", "tags": ["domain:test"]},
            {"topic_id": tid, "decision": "失敗", "reason": "理由", "tags": ["bad:ns"]},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["index"] == 1


# ========================================
# V5: 全件失敗で {created: [], errors: [...]} が返る
# ========================================


class TestV5AllFail:
    """V5: 全件失敗で {created: [], errors: [...]} が返る"""

    def test_add_logs_all_fail(self, temp_db):
        """全件が存在しないtopic_idの場合、全件エラー"""
        result = add_logs([
            {"topic_id": 99999, "content": "失敗1", "title": "失敗1"},
            {"topic_id": 99998, "content": "失敗2", "title": "失敗2"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 0
        assert len(result["errors"]) == 2
        assert result["errors"][0]["index"] == 0
        assert result["errors"][1]["index"] == 1

    def test_add_decisions_all_fail(self, temp_db):
        """全件が存在しないtopic_idの場合、全件エラー"""
        result = add_decisions([
            {"topic_id": 99999, "decision": "失敗1", "reason": "理由1"},
            {"topic_id": 99998, "decision": "失敗2", "reason": "理由2"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 0
        assert len(result["errors"]) == 2


# ========================================
# V6: 空配列でバリデーションエラー
# ========================================


class TestV6EmptyArray:
    """V6: 空配列でバリデーションエラー"""

    def test_add_logs_empty_array(self, temp_db):
        """空配列でVALIDATION_ERRORが返る"""
        result = add_logs([])

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "empty" in result["error"]["message"]

    def test_add_decisions_empty_array(self, temp_db):
        """空配列でVALIDATION_ERRORが返る"""
        result = add_decisions([])

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "empty" in result["error"]["message"]


# ========================================
# V7: 11件でバリデーションエラー
# ========================================


class TestV7ExceedLimit:
    """V7: 11件でバリデーションエラー"""

    def test_add_logs_exceed_limit(self, topic):
        """11件でVALIDATION_ERRORが返る"""
        items = [
            {"topic_id": topic["topic_id"], "content": f"ログ{i}", "title": f"タイトル{i}"}
            for i in range(11)
        ]
        result = add_logs(items)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "10" in result["error"]["message"]

    def test_add_decisions_exceed_limit(self, topic):
        """11件でVALIDATION_ERRORが返る"""
        items = [
            {"topic_id": topic["topic_id"], "decision": f"決定{i}", "reason": f"理由{i}"}
            for i in range(11)
        ]
        result = add_decisions(items)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "10" in result["error"]["message"]

    def test_add_logs_10_items_succeeds(self, topic):
        """10件は正常に登録される"""
        tid = topic["topic_id"]
        items = [
            {"topic_id": tid, "content": f"ログ{i}", "title": f"タイトル{i}"}
            for i in range(10)
        ]
        result = add_logs(items)

        assert "error" not in result
        assert len(result["created"]) == 10
        assert len(result["errors"]) == 0


# ========================================
# V8: title自動生成（\n/\\n対応）が動作する
# ========================================


class TestV8TitleAutoGenerate:
    """V8: title自動生成が動作する"""

    def test_add_logs_title_auto_from_content(self, topic):
        """title省略時にcontentの先頭行から自動生成"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "先頭行がタイトルになる\n2行目は含まない"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        assert result["created"][0]["title"] == "先頭行がタイトルになる"

    def test_add_logs_title_auto_literal_backslash_n(self, topic):
        """リテラルの\\nでも分割される"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "リテラル分割テスト\\nこの部分は含まない"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        assert result["created"][0]["title"] == "リテラル分割テスト"

    def test_add_logs_title_auto_truncate_50(self, topic):
        """50文字を超えるtitleは50文字で切り詰められる"""
        tid = topic["topic_id"]
        long_content = "あ" * 60
        result = add_logs([
            {"topic_id": tid, "content": long_content},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        assert len(result["created"][0]["title"]) == 50

    def test_add_logs_title_empty_content_empty_error(self, topic):
        """title未指定でcontent空の場合はエラー"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "正常ログ", "title": "正常"},
            {"topic_id": tid, "content": ""},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["index"] == 1

    def test_add_logs_explicit_title_unchanged(self, topic):
        """明示的にtitleを指定した場合は自動生成しない"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "別の内容", "title": "明示的タイトル"},
        ])

        assert "error" not in result
        assert result["created"][0]["title"] == "明示的タイトル"


# ========================================
# V9: created分のみembeddingが存在する
# ========================================


class TestV9EmbeddingCreatedOnly:
    """V9: created分のみembeddingが存在する"""

    def test_add_logs_embedding_for_created_only(self, topic, mock_embedding_server):
        """部分成功時、created分のみembeddingが生成される"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "成功ログ", "title": "成功"},
            {"topic_id": 99999, "content": "失敗ログ", "title": "失敗"},
        ])

        assert len(result["created"]) == 1
        assert len(result["errors"]) == 1

        # created分のembeddingが存在する
        log_id = result["created"][0]["log_id"]
        rows = execute_query(
            "SELECT id FROM search_index WHERE source_type = ? AND source_id = ?",
            ("log", log_id),
        )
        assert len(rows) > 0
        search_index_id = rows[0]["id"]

        conn = get_connection()
        try:
            cursor = conn.execute("SELECT count(*) FROM vec_index WHERE rowid = ?", (search_index_id,))
            count = cursor.fetchone()[0]
            assert count == 1
        finally:
            conn.close()

    def test_add_decisions_embedding_for_created_only(self, topic, mock_embedding_server):
        """部分成功時、created分のみembeddingが生成される"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "成功決定", "reason": "理由"},
            {"topic_id": 99999, "decision": "失敗決定", "reason": "理由"},
        ])

        assert len(result["created"]) == 1
        assert len(result["errors"]) == 1

        # created分のembeddingが存在する
        dec_id = result["created"][0]["decision_id"]
        rows = execute_query(
            "SELECT id FROM search_index WHERE source_type = ? AND source_id = ?",
            ("decision", dec_id),
        )
        assert len(rows) > 0
        search_index_id = rows[0]["id"]

        conn = get_connection()
        try:
            cursor = conn.execute("SELECT count(*) FROM vec_index WHERE rowid = ?", (search_index_id,))
            count = cursor.fetchone()[0]
            assert count == 1
        finally:
            conn.close()


# ========================================
# V10: tag_notesが全タグUNIONで注入される
# ========================================


class TestV10TagNotesUnion:
    """V10: tag_notesが全タグUNIONで注入される

    main.pyのMCPハンドラでのtag_notes注入ロジックをテストする。
    @mcp.tool()デコレータされた関数は直接呼べないため、
    _maybe_inject_tag_notesと全タグUNIONロジックを直接テストする。
    """

    def test_add_logs_tag_notes_union(self, temp_db):
        """全アイテムのタグUNIONでtag_notesが注入される"""
        from src.main import _maybe_inject_tag_notes

        # tag_notesを持つタグを作成
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "withnotes", "重要な教訓"),
            )
            conn.commit()
        finally:
            conn.close()

        topic = add_topic(title="テスト", description="テスト", tags=["domain:withnotes"])
        tid = topic["topic_id"]

        # サービス層を直接呼ぶ
        items = [
            {"topic_id": tid, "content": "ログ1", "title": "タイトル1", "tags": ["domain:withnotes"]},
            {"topic_id": tid, "content": "ログ2", "title": "タイトル2"},
        ]
        result = add_logs(items)
        assert "error" not in result

        # main.pyのハンドラが行うのと同じロジック: 全アイテムのタグUNION
        all_tags = set()
        for item in items:
            if item.get("tags"):
                all_tags.update(item["tags"])
        assert "domain:withnotes" in all_tags

        # tag_notes注入
        if all_tags:
            _maybe_inject_tag_notes(result, list(all_tags))

        assert "tag_notes" in result
        tag_note_tags = [tn["tag"] for tn in result["tag_notes"]]
        assert "domain:withnotes" in tag_note_tags

    def test_add_decisions_tag_notes_union(self, temp_db):
        """全アイテムのタグUNIONでtag_notesが注入される"""
        from src.main import _maybe_inject_tag_notes

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "withnotes2", "決定の教訓"),
            )
            conn.commit()
        finally:
            conn.close()

        topic = add_topic(title="テスト", description="テスト", tags=["domain:withnotes2"])
        tid = topic["topic_id"]

        items = [
            {"topic_id": tid, "decision": "決定1", "reason": "理由1", "tags": ["domain:withnotes2"]},
            {"topic_id": tid, "decision": "決定2", "reason": "理由2"},
        ]
        result = add_decisions(items)
        assert "error" not in result

        all_tags = set()
        for item in items:
            if item.get("tags"):
                all_tags.update(item["tags"])

        if all_tags:
            _maybe_inject_tag_notes(result, list(all_tags))

        assert "tag_notes" in result
        tag_note_tags = [tn["tag"] for tn in result["tag_notes"]]
        assert "domain:withnotes2" in tag_note_tags

    def test_tag_notes_union_multiple_tags(self, temp_db):
        """複数アイテムのタグがすべてUNIONされる"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "tag-a", "教訓A"),
            )
            conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "tag-b", "教訓B"),
            )
            conn.commit()
        finally:
            conn.close()

        # 全タグUNIONロジックのテスト
        items = [
            {"topic_id": 1, "content": "1", "tags": ["domain:tag-a"]},
            {"topic_id": 1, "content": "2", "tags": ["domain:tag-b"]},
            {"topic_id": 1, "content": "3"},  # tagsなし
        ]
        all_tags = set()
        for item in items:
            if item.get("tags"):
                all_tags.update(item["tags"])

        assert all_tags == {"domain:tag-a", "domain:tag-b"}


# ========================================
# 追加テスト: タグの継承と個別指定
# ========================================


class TestTagInheritance:
    """tags省略時のtopic継承と個別指定"""

    def test_add_logs_tags_inherited_from_topic(self, topic):
        """tags省略時にtopicのタグが継承される"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "タグ省略ログ", "title": "タグなし"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        # topicのタグ（domain:test）が継承される
        assert "domain:test" in result["created"][0]["tags"]

    def test_add_logs_tags_individual(self, topic):
        """個別タグ指定時はtopicタグとマージされる"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "タグ個別ログ", "title": "タグあり", "tags": ["intent:discuss"]},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        tags = result["created"][0]["tags"]
        assert "domain:test" in tags
        assert "intent:discuss" in tags

    def test_add_decisions_tags_inherited_from_topic(self, topic):
        """tags省略時にtopicのタグが継承される（レスポンスにはdecision_idのみ）"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "タグ省略決定", "reason": "理由"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        assert result["created"][0]["decision_id"] > 0
        # レスポンス軽量化: tagsは含まれない
        assert "tags" not in result["created"][0]

    def test_add_logs_mixed_topics(self, topic, topic2):
        """異なるtopic_idのアイテムが混在しても正しく処理される"""
        result = add_logs([
            {"topic_id": topic["topic_id"], "content": "トピック1ログ", "title": "T1"},
            {"topic_id": topic2["topic_id"], "content": "トピック2ログ", "title": "T2"},
        ])

        assert "error" not in result
        assert len(result["created"]) == 2
        assert result["created"][0]["topic_id"] == topic["topic_id"]
        assert result["created"][1]["topic_id"] == topic2["topic_id"]


# ========================================
# 追加テスト: SAVEPOINTによるアトミック性
# ========================================


class TestSavepointAtomicity:
    """SAVEPOINTによる部分ロールバックの確認"""

    def test_failed_item_does_not_affect_others_logs(self, topic):
        """失敗アイテムが成功アイテムに影響しない"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "1番目", "title": "成功1"},
            {"topic_id": 99999, "content": "2番目失敗"},
            {"topic_id": tid, "content": "3番目", "title": "成功3"},
        ])

        assert len(result["created"]) == 2
        assert len(result["errors"]) == 1
        assert result["errors"][0]["index"] == 1

        # DBに2件だけ存在することを確認 (親 topic は relations.belongs_to 経由)
        conn = get_connection()
        try:
            rows = conn.execute(
                """
                SELECT COUNT(*) FROM discussion_logs l
                JOIN relations r ON r.source_type='log' AND r.source_id=l.id
                                AND r.target_type='topic' AND r.relation_type='belongs_to'
                                AND r.target_id = ?
                """,
                (tid,),
            ).fetchone()
            assert rows[0] == 2
        finally:
            conn.close()

    def test_failed_item_does_not_affect_others_decisions(self, topic):
        """失敗アイテムが成功アイテムに影響しない"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "成功1", "reason": "理由"},
            {"topic_id": 99999, "decision": "失敗", "reason": "理由"},
            {"topic_id": tid, "decision": "成功2", "reason": "理由"},
        ])

        assert len(result["created"]) == 2
        assert len(result["errors"]) == 1

        conn = get_connection()
        try:
            rows = conn.execute(
                """
                SELECT COUNT(*) FROM decisions d
                JOIN relations r ON r.source_type='decision' AND r.source_id=d.id
                                AND r.target_type='topic' AND r.relation_type='belongs_to'
                                AND r.target_id = ?
                """,
                (tid,),
            ).fetchone()
            assert rows[0] == 2
        finally:
            conn.close()
