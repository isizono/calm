"""タグユーティリティのユニットテスト"""
import os
import tempfile
import pytest
import numpy as np
from src.db import init_database, get_connection
from src.services.tag_service import (
    parse_tag,
    validate_and_parse_tags,
    ensure_tag_ids,
    resolve_tag_ids,
    resolve_tags,
    link_tags,
    format_tags,
    get_entity_tags,
    get_effective_tags_batch,
    update_tag,
    demote_tag_notes,
    collect_tag_notes_for_injection,
    _TAG_NOTES_RATCHET_CEILING,
)
from src.services.topic_service import add_topic
from src.services.material_service import get_material
import src.services.embedding_service as emb


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


class MockRow:
    """sqlite3.Rowのモック（key-based access対応）"""
    def __init__(self, ns, name):
        self._data = {"namespace": ns, "name": name}
    def __getitem__(self, key):
        return self._data[key]


# ========================================
# parse_tag テスト
# ========================================


class TestParseTag:
    """parse_tagのテスト"""

    def test_namespace_with_name(self):
        """namespace付きタグをパースできる"""
        assert parse_tag("domain:calm") == ("domain", "calm")

    def test_bare_tag(self):
        """素タグをパースできる"""
        assert parse_tag("hooks") == ("", "hooks")

    def test_intent_namespace(self):
        """intent namespaceをパースできる"""
        assert parse_tag("intent:design") == ("intent", "design")

    def test_glossary_namespace(self):
        """glossary namespaceをパースできる (D#2787 ユビキタス言語 namespace)"""
        assert parse_tag("glossary:ow") == ("glossary", "ow")

    def test_layer_namespace(self):
        """layer namespaceをパースできる（方向性decisionの区別用）"""
        assert parse_tag("layer:direction") == ("layer", "direction")

    def test_colon_in_name(self):
        """nameにコロンが含まれる場合、最初のコロンで分割"""
        assert parse_tag("domain:cc:memory") == ("domain", "cc:memory")

    def test_empty_colon(self):
        """コロンだけの場合"""
        assert parse_tag(":value") == ("", "value")


# ========================================
# validate_and_parse_tags テスト
# ========================================


class TestValidateAndParseTags:
    """validate_and_parse_tagsのテスト"""

    def test_valid_tags(self):
        """正常なタグ配列をパースできる"""
        result = validate_and_parse_tags(["domain:calm", "hooks", "intent:design"])
        assert isinstance(result, list)
        assert len(result) == 3
        assert ("domain", "calm") in result
        assert ("", "hooks") in result
        assert ("intent", "design") in result

    def test_deduplicate(self):
        """重複タグを排除する"""
        result = validate_and_parse_tags(["domain:test", "domain:test", "hooks", "hooks"])
        assert isinstance(result, list)
        assert len(result) == 2

    def test_skip_empty_strings(self):
        """空文字タグをスキップする"""
        result = validate_and_parse_tags(["domain:test", "", "  ", "hooks"])
        assert isinstance(result, list)
        assert len(result) == 2

    def test_required_empty_list(self):
        """required=Trueで空配列はTAGS_REQUIREDエラー"""
        result = validate_and_parse_tags([], required=True)
        assert isinstance(result, dict)
        assert result["error"]["code"] == "TAGS_REQUIRED"

    def test_required_all_empty_strings(self):
        """required=Trueで全部空文字もTAGS_REQUIREDエラー"""
        result = validate_and_parse_tags(["", "  "], required=True)
        assert isinstance(result, dict)
        assert result["error"]["code"] == "TAGS_REQUIRED"

    def test_required_false_empty_ok(self):
        """required=Falseで空配列は空リスト"""
        result = validate_and_parse_tags([])
        assert isinstance(result, list)
        assert result == []

    def test_glossary_namespace_accepted(self):
        """glossary namespaceが受理される (D#2787 ユビキタス言語 namespace)"""
        result = validate_and_parse_tags(["glossary:ow", "glossary:cc-memory"])
        assert isinstance(result, list)
        assert ("glossary", "ow") in result
        assert ("glossary", "cc-memory") in result

    def test_layer_namespace_accepted(self):
        """layer namespaceが受理される（方向性decisionの区別用）"""
        result = validate_and_parse_tags(["layer:direction"])
        assert isinstance(result, list)
        assert ("layer", "direction") in result

    def test_invalid_namespace(self):
        """不正なnamespaceでINVALID_TAG_NAMESPACEエラー"""
        result = validate_and_parse_tags(["invalid:tag"])
        assert isinstance(result, dict)
        assert result["error"]["code"] == "INVALID_TAG_NAMESPACE"

    def test_empty_name(self):
        """name空文字でINVALID_TAG_NAMEエラー"""
        result = validate_and_parse_tags(["domain:"])
        assert isinstance(result, dict)
        assert result["error"]["code"] == "INVALID_TAG_NAME"

    def test_whitespace_name(self):
        """nameが空白のみでINVALID_TAG_NAMEエラー"""
        result = validate_and_parse_tags(["domain:   "])
        assert isinstance(result, dict)
        assert result["error"]["code"] == "INVALID_TAG_NAME"


# ========================================
# ensure_tag_ids テスト
# ========================================


class TestEnsureTagIds:
    """ensure_tag_idsのテスト"""

    def test_create_new_tags(self, temp_db):
        """新規タグをINSERT OR IGNOREしてIDを返す"""
        conn = get_connection()
        try:
            tag_ids = ensure_tag_ids(conn, [("domain", "test"), ("", "hooks")])
            conn.commit()
            assert len(tag_ids) == 2
            assert all(isinstance(tid, int) for tid in tag_ids)
        finally:
            conn.close()

    def test_idempotent(self, temp_db):
        """同じタグを2回呼んでも同じIDが返る"""
        conn = get_connection()
        try:
            ids1 = ensure_tag_ids(conn, [("domain", "test")])
            ids2 = ensure_tag_ids(conn, [("domain", "test")])
            conn.commit()
            assert ids1 == ids2
        finally:
            conn.close()

    def test_empty_list(self, temp_db):
        """空リストを渡すと空リストが返る"""
        conn = get_connection()
        try:
            tag_ids = ensure_tag_ids(conn, [])
            assert tag_ids == []
        finally:
            conn.close()

    def test_preserves_order(self, temp_db):
        """入力順序が保持される"""
        conn = get_connection()
        try:
            tags = [("intent", "design"), ("domain", "alpha"), ("", "zebra")]
            tag_ids = ensure_tag_ids(conn, tags)
            conn.commit()
            assert len(tag_ids) == 3
            # 各IDが異なることを確認
            assert len(set(tag_ids)) == 3
            # 再度呼んで同じ順序で返ることを確認
            tag_ids2 = ensure_tag_ids(conn, tags)
            assert tag_ids == tag_ids2
        finally:
            conn.close()


# ========================================
# resolve_tag_ids テスト
# ========================================


class TestResolveTagIds:
    """resolve_tag_idsのテスト"""

    def test_existing_tags(self, temp_db):
        """存在するタグのIDが正しく返る"""
        conn = get_connection()
        try:
            # まずタグを作成
            ensure_tag_ids(conn, [("domain", "test"), ("", "hooks")])
            conn.commit()

            # resolve_tag_idsで取得
            tag_ids = resolve_tag_ids(conn, [("domain", "test"), ("", "hooks")])
            assert len(tag_ids) == 2
            assert all(isinstance(tid, int) for tid in tag_ids)
        finally:
            conn.close()

    def test_nonexistent_tags(self, temp_db):
        """存在しないタグで空リストが返る"""
        conn = get_connection()
        try:
            tag_ids = resolve_tag_ids(conn, [("domain", "nonexistent"), ("", "missing")])
            assert tag_ids == []
        finally:
            conn.close()

    def test_partial_match(self, temp_db):
        """一部だけ存在する場合、存在するもののIDのみ返る"""
        conn = get_connection()
        try:
            # 1つだけタグを作成
            ensure_tag_ids(conn, [("domain", "test")])
            conn.commit()

            # 存在するものと存在しないものを混ぜて渡す
            tag_ids = resolve_tag_ids(conn, [("domain", "test"), ("domain", "nonexistent")])
            assert len(tag_ids) == 1
            assert isinstance(tag_ids[0], int)
        finally:
            conn.close()

    def test_empty_input(self, temp_db):
        """空リスト入力で空リストが返る"""
        conn = get_connection()
        try:
            tag_ids = resolve_tag_ids(conn, [])
            assert tag_ids == []
        finally:
            conn.close()


# ========================================
# link_tags テスト
# ========================================


class TestLinkTags:
    """link_tagsのテスト"""

    def test_link_tags_to_topic(self, temp_db):
        """topic_tagsにタグを紐付けできる"""
        from src.services.topic_service import add_topic
        topic = add_topic(title="Test", description="Test", tags=["domain:test"])

        conn = get_connection()
        try:
            # 追加のタグをリンク
            new_tag_ids = ensure_tag_ids(conn, [("", "extra")])
            link_tags(conn, "topic_tags", "topic_id", topic["topic_id"], new_tag_ids)
            conn.commit()

            # 確認
            tags = get_entity_tags(conn, "topic_tags", "topic_id", topic["topic_id"])
            assert "extra" in tags
            assert "domain:test" in tags
        finally:
            conn.close()


# ========================================
# format_tags テスト
# ========================================


class TestFormatTags:
    """format_tagsのテスト"""

    def test_namespace_and_bare(self):
        """namespace付きと素タグの混在"""
        rows = [
            MockRow("domain", "calm"),
            MockRow("", "hooks"),
            MockRow("intent", "design"),
        ]
        result = format_tags(rows)
        assert result == ["domain:calm", "hooks", "intent:design"]

    def test_sorted(self):
        """アルファベット順ソート"""
        rows = [
            MockRow("", "zebra"),
            MockRow("domain", "alpha"),
            MockRow("", "beta"),
        ]
        result = format_tags(rows)
        assert result == ["beta", "domain:alpha", "zebra"]

    def test_empty(self):
        """空のrows"""
        result = format_tags([])
        assert result == []


# ========================================
# get_effective_tags_batch テスト
# ========================================


class TestGetEffectiveTagsBatch:
    """get_effective_tags_batchのテスト"""

    def test_batch_returns_topic_tags(self, temp_db):
        """topicのタグがdecisionに継承される"""
        from src.services.topic_service import add_topic
        from tests.helpers import add_decision

        topic = add_topic(title="Test", description="Test", tags=["domain:test"])
        dec = add_decision(
            topic_id=topic["topic_id"],
            decision="Dec 1",
            reason="Reason 1",
        )

        conn = get_connection()
        try:
            result = get_effective_tags_batch(conn, "decision", topic["topic_id"])
            assert dec["decision_id"] in result
            assert "domain:test" in result[dec["decision_id"]]
        finally:
            conn.close()

    def test_batch_includes_entity_tags(self, temp_db):
        """entity個別タグも含まれる"""
        from src.services.topic_service import add_topic
        from tests.helpers import add_decision

        topic = add_topic(title="Test", description="Test", tags=["domain:test"])
        dec = add_decision(
            topic_id=topic["topic_id"],
            decision="Dec 1",
            reason="Reason 1",
            tags=["intent:design"],
        )

        conn = get_connection()
        try:
            result = get_effective_tags_batch(conn, "decision", topic["topic_id"])
            assert dec["decision_id"] in result
            tags = result[dec["decision_id"]]
            assert "domain:test" in tags
            assert "intent:design" in tags
        finally:
            conn.close()

    def test_batch_empty_topic(self, temp_db):
        """entity 0件のtopicでは空dictが返る"""
        from src.services.topic_service import add_topic

        topic = add_topic(title="Empty", description="Test", tags=["domain:test"])

        conn = get_connection()
        try:
            result = get_effective_tags_batch(conn, "decision", topic["topic_id"])
            assert result == {}
        finally:
            conn.close()

    def test_batch_multiple_entities(self, temp_db):
        """複数entityのタグを一括取得"""
        from src.services.topic_service import add_topic
        from tests.helpers import add_log

        topic = add_topic(title="Test", description="Test", tags=["domain:test"])
        log1 = add_log(topic_id=topic["topic_id"], title="Log 1", content="Content 1")
        log2 = add_log(
            topic_id=topic["topic_id"],
            title="Log 2",
            content="Content 2",
            tags=["extra"],
        )

        conn = get_connection()
        try:
            result = get_effective_tags_batch(conn, "log", topic["topic_id"])
            assert log1["log_id"] in result
            assert log2["log_id"] in result
            assert "domain:test" in result[log1["log_id"]]
            assert "domain:test" in result[log2["log_id"]]
            assert "extra" in result[log2["log_id"]]
        finally:
            conn.close()


# ========================================
# resolve_tags テスト
# ========================================

EMBEDDING_DIM = 384


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


@pytest.fixture
def mock_embedding_server_down(monkeypatch):
    """embeddingサーバーダウン状態をシミュレート"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)
    yield


class TestResolveTags:
    """resolve_tagsのテスト"""

    def test_exact_match(self, temp_db, mock_embedding_server):
        """完全一致 → 既存IDを使用、merged_tagsなし"""
        # まずタグを作成
        conn = get_connection()
        try:
            ids = ensure_tag_ids(conn, [("", "hook")])
            conn.commit()
        finally:
            conn.close()
        # embeddingも生成
        emb.generate_and_store_tag_embedding(ids[0], "hook")

        result = resolve_tags(["hook"])
        assert not isinstance(result, dict), f"Expected tuple, got error: {result}"
        tag_ids, merged_tags = result
        assert len(tag_ids) == 1
        assert merged_tags == []

    def test_brand_new_tag(self, temp_db, mock_embedding_server):
        """新規タグ → 新規作成、merged_tags空"""
        result = resolve_tags(["brand-new-tag"])
        assert not isinstance(result, dict), f"Expected tuple, got error: {result}"
        tag_ids, merged_tags = result
        assert len(tag_ids) == 1
        assert merged_tags == []

        # DBにタグが存在する
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id, name FROM tags WHERE namespace = '' AND name = 'brand-new-tag'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_fuzzy_match_merges(self, temp_db, monkeypatch):
        """類似タグ → 既存タグにマージ → merged_tagsに情報あり"""
        # "hook"(document)と"hooks"(query)で同一ベクトルを返すモック
        # → distance=0 で確実にMERGE_THRESHOLD(0.15)未満になる
        fixed_vector = np.ones(EMBEDDING_DIM, dtype=np.float32) / np.sqrt(EMBEDDING_DIM)
        fixed_vector = fixed_vector.tolist()

        def mock_encode_batch(texts, prefix):
            return [fixed_vector for _ in texts]

        monkeypatch.setattr(emb, '_encode_batch', mock_encode_batch)
        monkeypatch.setattr(emb, '_server_initialized', True)
        monkeypatch.setattr(emb, '_backfill_done', True)

        # "hook" タグを作成 + embedding格納
        conn = get_connection()
        try:
            ids = ensure_tag_ids(conn, [("", "hook")])
            conn.commit()
        finally:
            conn.close()
        emb.generate_and_store_tag_embedding(ids[0], "hook")

        # "hooks" を解決 → "hook" にマージされるべき
        result = resolve_tags(["hooks"])
        assert not isinstance(result, dict), f"Expected tuple, got error: {result}"
        tag_ids, merged_tags = result
        assert len(tag_ids) == 1
        assert len(merged_tags) == 1
        assert merged_tags[0]["input"] == "hooks"
        assert merged_tags[0]["merged_to"] == "hook"
        assert merged_tags[0]["distance"] < 0.15

    def test_force_new_tags_skips_knn(self, temp_db, mock_embedding_server):
        """force_new_tags=True + 完全一致なし → 新規作成、マージされない"""
        # "hook" タグを作成 + embedding格納
        conn = get_connection()
        try:
            ids = ensure_tag_ids(conn, [("", "hook")])
            conn.commit()
        finally:
            conn.close()
        emb.generate_and_store_tag_embedding(ids[0], "hook")

        result = resolve_tags(["hooks"], force_new_tags=True)
        assert not isinstance(result, dict), f"Expected tuple, got error: {result}"
        tag_ids, merged_tags = result
        assert len(tag_ids) == 1
        assert merged_tags == []  # マージされない

        # "hooks" が新規タグとして作成されている
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM tags WHERE namespace = '' AND name = 'hooks'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_force_new_tags_exact_match_uses_existing(self, temp_db, mock_embedding_server):
        """force_new_tags=True + 完全一致 "hook" → 既存ID使用（新規作成しない）"""
        conn = get_connection()
        try:
            ids = ensure_tag_ids(conn, [("", "hook")])
            conn.commit()
            original_id = ids[0]
        finally:
            conn.close()

        result = resolve_tags(["hook"], force_new_tags=True)
        assert not isinstance(result, dict), f"Expected tuple, got error: {result}"
        tag_ids, merged_tags = result
        assert len(tag_ids) == 1
        assert tag_ids[0] == original_id
        assert merged_tags == []

    def test_namespace_normalization(self, temp_db, mock_embedding_server):
        """"Domain:CC-Memory" → 正規化 → namespace="domain", name="cc-memory" で解決"""
        result = resolve_tags(["Domain:CC-Memory"])
        assert not isinstance(result, dict), f"Expected tuple, got error: {result}"
        tag_ids, merged_tags = result
        assert len(tag_ids) == 1

        # 正規化されたタグがDBに存在
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT namespace, name FROM tags WHERE id = ?",
                (tag_ids[0],),
            ).fetchone()
            assert row["namespace"] == "domain"
            assert row["name"] == "cc-memory"
        finally:
            conn.close()

    def test_empty_tag_error(self, temp_db, mock_embedding_server):
        """空文字列タグ → 空リスト（validate_and_parse_tagsが空文字をスキップ）"""
        result = resolve_tags([""])
        assert not isinstance(result, dict), f"Expected tuple, got error: {result}"
        tag_ids, merged_tags = result
        assert tag_ids == []
        assert merged_tags == []

    def test_invalid_namespace_error(self, temp_db, mock_embedding_server):
        """不正namespace → バリデーションエラー"""
        result = resolve_tags(["unknown:foo"])
        assert isinstance(result, dict)
        assert result["error"]["code"] == "INVALID_TAG_NAMESPACE"

    def test_duplicate_input_deduplication(self, temp_db, mock_embedding_server):
        """["hooks", "hooks"] → IDリストに1つだけ"""
        result = resolve_tags(["hooks", "hooks"])
        assert not isinstance(result, dict), f"Expected tuple, got error: {result}"
        tag_ids, merged_tags = result
        assert len(tag_ids) == 1

    def test_embedding_server_down_exact_match_works(self, temp_db, mock_embedding_server_down):
        """embeddingサーバーダウン → 完全一致のみで動作、エラーにならない"""
        # まず完全一致用のタグを作成（サーバーダウン前に作れないのでSQLで直接）
        conn = get_connection()
        try:
            conn.execute("INSERT INTO tags (namespace, name) VALUES ('', 'hook')")
            conn.commit()
        finally:
            conn.close()

        result = resolve_tags(["hook"])
        assert not isinstance(result, dict), f"Expected tuple, got error: {result}"
        tag_ids, merged_tags = result
        assert len(tag_ids) == 1
        assert merged_tags == []

    def test_embedding_server_down_new_tag_created(self, temp_db, mock_embedding_server_down):
        """embeddingサーバーダウン → 完全一致なしの場合は新規作成（KNN空結果）"""
        result = resolve_tags(["brand-new"])
        assert not isinstance(result, dict), f"Expected tuple, got error: {result}"
        tag_ids, merged_tags = result
        assert len(tag_ids) == 1
        assert merged_tags == []

    def test_empty_tag_vec_creates_new(self, temp_db, mock_embedding_server):
        """空のtag_vec → 全て新規作成"""
        result = resolve_tags(["alpha", "beta", "gamma"])
        assert not isinstance(result, dict), f"Expected tuple, got error: {result}"
        tag_ids, merged_tags = result
        assert len(tag_ids) == 3
        assert len(set(tag_ids)) == 3  # 全て異なるID

    def test_empty_input_list(self, temp_db, mock_embedding_server):
        """空リスト入力 → 空リストが返る"""
        result = resolve_tags([])
        assert not isinstance(result, dict), f"Expected tuple, got error: {result}"
        tag_ids, merged_tags = result
        assert tag_ids == []
        assert merged_tags == []


# ========================================
# embedding_service tag ヘルパーのテスト
# ========================================


class TestTagEmbeddingHelpers:
    """embedding_serviceのtag embeddingヘルパーのテスト"""

    def test_generate_and_store_tag_embedding(self, temp_db, mock_embedding_server):
        """generate_and_store_tag_embedding: tag_vecにembeddingが格納される"""
        conn = get_connection()
        try:
            ids = ensure_tag_ids(conn, [("", "test-tag")])
            conn.commit()
            tag_id = ids[0]
        finally:
            conn.close()

        emb.generate_and_store_tag_embedding(tag_id, "test-tag")

        conn = get_connection()
        try:
            cursor = conn.execute("SELECT count(*) FROM tag_vec WHERE rowid = ?", (tag_id,))
            count = cursor.fetchone()[0]
            assert count == 1
        finally:
            conn.close()

    def test_search_similar_tags(self, temp_db, mock_embedding_server):
        """search_similar_tags: 格納済みtagのKNN検索ができる"""
        conn = get_connection()
        try:
            ids = ensure_tag_ids(conn, [("", "hook"), ("", "design"), ("", "testing")])
            conn.commit()
        finally:
            conn.close()

        for tag_id, name in zip(ids, ["hook", "design", "testing"]):
            emb.generate_and_store_tag_embedding(tag_id, name)

        results = emb.search_similar_tags("hooks", k=3)
        assert isinstance(results, list)
        assert len(results) > 0
        # 各結果は (tag_id, distance) のタプル
        for tag_id, distance in results:
            assert isinstance(tag_id, int)
            assert isinstance(distance, float)

    def test_search_similar_tags_server_down(self, temp_db, mock_embedding_server_down):
        """search_similar_tags: サーバーダウン時は空リストを返す"""
        results = emb.search_similar_tags("hooks")
        assert results == []

    def test_backfill_tag_embeddings(self, temp_db, mock_embedding_server, monkeypatch):
        """backfill_tag_embeddings: tag_vecが空のタグにembeddingを一括生成"""
        monkeypatch.setattr(emb, '_is_server_running', lambda: True)

        # タグを作成（embeddingは生成しない）
        conn = get_connection()
        try:
            ids = ensure_tag_ids(conn, [("", "alpha"), ("", "beta")])
            conn.commit()
        finally:
            conn.close()

        # init_database由来のタグも含めてバックフィル
        filled = emb.backfill_tag_embeddings()
        assert filled >= 2  # 少なくともalpha, betaの2つ

        # tag_vecにembeddingが存在する
        conn = get_connection()
        try:
            for tag_id in ids:
                cursor = conn.execute("SELECT count(*) FROM tag_vec WHERE rowid = ?", (tag_id,))
                count = cursor.fetchone()[0]
                assert count == 1
        finally:
            conn.close()

    def test_backfill_tag_embeddings_noop(self, temp_db, mock_embedding_server, monkeypatch):
        """backfill_tag_embeddings: 全タグにembeddingがある場合は0を返す"""
        monkeypatch.setattr(emb, '_is_server_running', lambda: True)

        # init_database由来のタグを先にバックフィル
        emb.backfill_tag_embeddings()

        # タグ作成＋embedding生成
        conn = get_connection()
        try:
            ids = ensure_tag_ids(conn, [("", "gamma")])
            conn.commit()
        finally:
            conn.close()
        emb.generate_and_store_tag_embedding(ids[0], "gamma")

        # 再バックフィルで0を返す
        filled = emb.backfill_tag_embeddings()
        assert filled == 0

    def test_generate_and_store_tag_embedding_server_down(self, temp_db, mock_embedding_server_down):
        """generate_and_store_tag_embedding: サーバーダウン時は何もしない"""
        conn = get_connection()
        try:
            conn.execute("INSERT INTO tags (namespace, name) VALUES ('', 'test-tag')")
            conn.commit()
            row = conn.execute("SELECT id FROM tags WHERE name = 'test-tag'").fetchone()
            tag_id = row["id"]
        finally:
            conn.close()

        # エラーにならない
        emb.generate_and_store_tag_embedding(tag_id, "test-tag")

        # tag_vecにはembeddingがない
        conn = get_connection()
        try:
            cursor = conn.execute("SELECT count(*) FROM tag_vec WHERE rowid = ?", (tag_id,))
            count = cursor.fetchone()[0]
            assert count == 0
        finally:
            conn.close()


# ========================================
# demote_tag_notes テスト
# ========================================


def _get_tag_notes(tag_str: str) -> str:
    """テスト用: 指定タグの現在のnotesを取得する（NULLなら""）。"""
    namespace, name = parse_tag(tag_str)
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT notes FROM tags WHERE namespace = ? AND name = ?",
            (namespace, name),
        ).fetchone()
    finally:
        conn.close()
    return (row["notes"] or "") if row else ""


def _count_materials() -> int:
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM materials").fetchone()["c"]
    finally:
        conn.close()


class TestDemoteTagNotesDocstringSync:
    """main.pyのツールdocstringとtag_service側の実処理docstringが同一であること
    (二層のうち片方だけ更新される事故が過去に起きているための導出型整合性lint)"""

    def test_tool_and_service_docstrings_are_identical(self):
        import inspect
        from src.main import demote_tag_notes as tool_fn

        assert inspect.getdoc(tool_fn) == inspect.getdoc(demote_tag_notes)


class TestDemoteTagNotes:
    """demote_tag_notesのテスト"""

    @pytest.fixture(autouse=True)
    def _disable_embedding_for_this_class(self, disable_embedding):
        """このクラスの全テストでembedding呼び出しを無効化する"""

    def test_tag_not_found(self, temp_db):
        result = demote_tag_notes("domain:nonexistent", sections=["X"])
        assert result["error"]["code"] == "NOT_FOUND"

    def test_empty_sections_is_validation_error(self, temp_db):
        add_topic(title="T", description="D", tags=["domain:test"])
        result = demote_tag_notes("domain:test", sections=[])
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_mode_is_validation_error(self, temp_db):
        add_topic(title="T", description="D", tags=["domain:test"])
        update_tag("domain:test", notes="## A\n本文\n")
        result = demote_tag_notes("domain:test", sections=["A"], mode="delete")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_no_heading_at_all_returns_section_not_found(self, temp_db):
        """前文型タグ('## '見出しが1つも無い)でdemoteを試みるとSECTION_NOT_FOUND"""
        add_topic(title="T", description="D", tags=["domain:test"])
        update_tag("domain:test", notes="前文だけのnotes")
        result = demote_tag_notes("domain:test", sections=["前文だけのnotes"])
        assert result["error"]["code"] == "SECTION_NOT_FOUND"

    def test_preamble_cannot_be_demoted(self, temp_db):
        """最初の '## ' より前の前文は退避対象にできない(見出しとして指定してもSECTION_NOT_FOUND)"""
        add_topic(title="T", description="D", tags=["domain:test"])
        update_tag("domain:test", notes="前文だよ\n\n## セクションA\n本文A\n")
        result = demote_tag_notes("domain:test", sections=["前文だよ"])
        assert result["error"]["code"] == "SECTION_NOT_FOUND"

    def test_ambiguous_section_when_heading_duplicated(self, temp_db):
        """同一見出しが複数あるタグではAMBIGUOUS_SECTIONで拒否し、notesは変更しない"""
        add_topic(title="T", description="D", tags=["domain:test"])
        original = "## X\n本文1\n\n## X\n本文2\n"
        update_tag("domain:test", notes=original)
        result = demote_tag_notes("domain:test", sections=["X"])
        assert result["error"]["code"] == "AMBIGUOUS_SECTION"
        assert _get_tag_notes("domain:test") == original

    def test_re_demoting_same_section_returns_section_not_found(self, temp_db):
        add_topic(title="T", description="D", tags=["domain:test"])
        update_tag("domain:test", notes="## A\n本文A\n\n## B\n本文B\n")
        first = demote_tag_notes("domain:test", sections=["A"], mode="drop")
        assert "error" not in first
        second = demote_tag_notes("domain:test", sections=["A"], mode="drop")
        assert second["error"]["code"] == "SECTION_NOT_FOUND"

    def test_trailer_marker_is_preserved_after_demote(self, temp_db):
        """末尾の素マーカー(#audited-...)は退避後もnotesに残る"""
        add_topic(title="T", description="D", tags=["domain:test"])
        update_tag("domain:test", notes="## A\n本文A\n\n#audited-2026-09-04\n")
        result = demote_tag_notes("domain:test", sections=["A"], mode="drop")
        assert "error" not in result
        assert _get_tag_notes("domain:test").endswith("#audited-2026-09-04\n")

    def test_trailer_dated_hint_cooldown_marker_is_preserved_after_demote(self, temp_db):
        """hint_serviceが実際に書き込むコロン付き日次クールダウンマーカー
        (例: #recompose-delta-skipped-until:YYYY-MM-DD)も末尾trailerとして
        退避後に残る。マーカーの実際の書式はhint_service側から導出し、
        本テストではハードコードしない(audit重複防止とhintクールダウンが
        同時に壊れる最重要ケースの一つ)。"""
        from datetime import date
        from src.services.hint_service import MARKER_RECOMPOSE_DELTA, _merge_cooldown_marker

        marker_line = _merge_cooldown_marker("", MARKER_RECOMPOSE_DELTA, date(2026, 9, 4))

        add_topic(title="T", description="D", tags=["domain:test"])
        update_tag("domain:test", notes=f"## A\n本文A\n\n{marker_line}\n")

        result = demote_tag_notes("domain:test", sections=["A"], mode="drop")
        assert "error" not in result
        assert _get_tag_notes("domain:test").endswith(f"{marker_line}\n")

    def test_remaining_sections_concatenation_is_byte_identical_to_original(self, temp_db):
        """dropモードで退避後、残ったセクションの連結が退避前の対応部分とバイト単位で同一"""
        add_topic(title="T", description="D", tags=["domain:test"])
        preamble = "前文だよ\n\n"
        block_a = "## セクションA\n本文A\n\n"
        block_b = "## セクションB\n本文B\n\n"
        block_c = "## セクションC\n本文C\n"
        original_notes = preamble + block_a + block_b + block_c
        update_tag("domain:test", notes=original_notes)

        result = demote_tag_notes("domain:test", sections=["セクションB"], mode="drop")
        assert "error" not in result

        expected_remaining = preamble + block_a + block_c
        assert _get_tag_notes("domain:test") == expected_remaining

    def test_all_sections_demoted_results_in_empty_notes_and_excluded_from_injection(self, temp_db):
        """全セクションをdropモードで退避し尽くすとnotesは空文字列になり、
        collect_tag_notes_for_injectionの対象からも外れる(NULL判定だけでは拾えないケース)"""
        add_topic(title="T", description="D", tags=["domain:test"])
        update_tag("domain:test", notes="## A\n本文A\n")
        result = demote_tag_notes("domain:test", sections=["A"], mode="drop")
        assert "error" not in result
        assert _get_tag_notes("domain:test") == ""

        conn = get_connection()
        try:
            notes = collect_tag_notes_for_injection(conn, ["domain:test"], mark=False)
        finally:
            conn.close()
        assert notes is None

    def test_archive_material_id_with_retracted_material_is_validation_error(self, temp_db):
        from src.services.material_service import add_material
        from src.services.retract_service import retract

        add_topic(title="T", description="D", tags=["domain:test"])
        update_tag("domain:test", notes="## A\n本文A\n")
        m = add_material(title="退避先候補", content="本文", tags=["domain:test"], source="s")
        retract("material", [m["material_id"]])

        result = demote_tag_notes(
            "domain:test", sections=["A"], archive_material_id=m["material_id"]
        )
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_archive_material_id_nonexistent_is_validation_error(self, temp_db):
        add_topic(title="T", description="D", tags=["domain:test"])
        update_tag("domain:test", notes="## A\n本文A\n")
        result = demote_tag_notes("domain:test", sections=["A"], archive_material_id=999999)
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_new_material_created_with_pointer_and_notes_shrunk(self, temp_db):
        add_topic(title="T", description="D", tags=["domain:test"])
        # 索引セクション追加分のオーバーヘッド(見出し+ポインタ行、約50字)より
        # 十分大きい本文を退避し、notes全体としては縮小することを確認する
        long_body = "退避される本文。" * 30
        update_tag(
            "domain:test",
            notes=f"前文\n\n## セクションA\n{long_body}\n\n## セクションB\n残る本文\n",
        )

        result = demote_tag_notes("domain:test", sections=["セクションA"], reason="整理のため")
        assert "error" not in result
        assert result["material_created"] is True
        assert result["demoted_sections"] == ["セクションA"]
        assert result["pointers_added"] == 1
        assert result["citations_converted"] == 0
        assert result["notes_length"]["before"] > result["notes_length"]["after"]
        assert result["notes_length"]["over_budget"] is False

        material = get_material(result["material_id"])
        assert "退避される本文" in material["content"]
        assert "整理のため" in material["content"]
        assert set(material["tags"]) >= {"domain:test", "tag-notes-archive"}
        assert material["title"].startswith("tag notes退避: ")

        new_notes = _get_tag_notes("domain:test")
        assert "退避される本文" not in new_notes
        assert "残る本文" in new_notes
        assert "セクションA → get_material(material_id=" in new_notes

    def test_pointer_lines_accumulate_across_multiple_demote_calls(self, temp_db):
        """複数回のdemoteのポインタが1つの索引セクションに集約され、重複しない"""
        add_topic(title="T", description="D", tags=["domain:test"])
        update_tag("domain:test", notes="## A\n本文A\n\n## B\n本文B\n\n## C\n本文C\n")

        r1 = demote_tag_notes("domain:test", sections=["A"])
        assert "error" not in r1
        r2 = demote_tag_notes("domain:test", sections=["B"])
        assert "error" not in r2
        assert r2["pointers_added"] == 1

        notes = _get_tag_notes("domain:test")
        assert notes.count("## 退避済み") == 1
        assert "A → get_material(material_id=" in notes
        assert "B → get_material(material_id=" in notes
        assert "本文C" in notes

    def test_append_mode_appends_to_existing_archive_material(self, temp_db):
        from src.services.material_service import add_material

        add_topic(title="T", description="D", tags=["domain:test"])
        update_tag("domain:test", notes="## A\n本文A\n\n## B\n本文B\n")
        archive = add_material(
            title="既存の退避先", content="既存の退避内容", tags=["domain:test"], source="s",
        )
        archive_id = archive["material_id"]

        result = demote_tag_notes("domain:test", sections=["A"], archive_material_id=archive_id)
        assert "error" not in result
        assert result["material_created"] is False
        assert result["material_id"] == archive_id

        material = get_material(archive_id)
        assert "既存の退避内容" in material["content"]
        assert "本文A" in material["content"]

    def test_archive_title_is_truncated_to_title_max_len(self, temp_db):
        from src.services.title_validation import TITLE_MAX_LEN

        long_tag_name = "x" * 60
        add_topic(title="T", description="D", tags=[f"domain:{long_tag_name}"])
        update_tag(f"domain:{long_tag_name}", notes="## A\n本文A\n")

        result = demote_tag_notes(f"domain:{long_tag_name}", sections=["A"])
        assert "error" not in result
        assert len(result["material_title"]) <= TITLE_MAX_LEN

    def test_ratchet_ceiling_rejection_rolls_back_entire_transaction(self, temp_db):
        """索引セクション追加分だけ長さが増えて4000字を超える場合、notesの書き込みが
        DBトリガーで拒否され、退避先資材の作成を含む変更全体がロールバックされる
        (資材は作られずに残らない)。エラーはupdate_tagの事前チェックと同じ理由
        (生SQLiteメッセージを露出させない)でVALIDATION_ERRORに正規化され、
        整理ループが使えるようbefore/afterの実測文字数もメッセージに含む。"""
        add_topic(title="T", description="D", tags=["domain:test"])

        tiny_section = "## S\ny\n"
        target_existing_len = 3995
        huge_prefix = "## Big\n"
        huge_suffix = "\n"
        pad_len = target_existing_len - len(tiny_section) - len(huge_prefix) - len(huge_suffix)
        huge_section = huge_prefix + ("x" * pad_len) + huge_suffix
        existing_notes = huge_section + tiny_section
        assert len(existing_notes) == target_existing_len

        set_result = update_tag("domain:test", notes=existing_notes)
        assert "error" not in set_result

        materials_before = _count_materials()

        result = demote_tag_notes("domain:test", sections=["S"], mode="pointer")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert str(target_existing_len) in result["error"]["message"]
        # notesはロールバックされ変更前のまま
        assert _get_tag_notes("domain:test") == existing_notes
        # 退避先資材は作られずに残る
        assert _count_materials() == materials_before

    def test_shrinking_update_succeeds_and_reports_over_budget_when_still_over_ceiling(self, temp_db):
        """4000字超過状態のタグ(migration 0066適用前から存在した既存データを想定)に
        対しては、縮む更新である限りdemoteが成功する(ラチェット則は増加のみを拒否し、
        縮小は天井超過中でも常に許可する)。縮小後もなお天井を超えていれば
        notes_length.over_budgetがTrueで返ることを確認する。

        migration 0066のUPDATEトリガーは増加更新のみを拒否するため、超過済みの
        notesを直接UPDATEで仕込むにはトリガーを一旦外す必要がある(このテストの
        前提データ作成のみに使う一時的な操作で、demote_tag_notes自体の呼び出しは
        トリガーが有効な状態で行う)。
        """
        add_topic(title="T", description="D", tags=["domain:test"])
        namespace, name = parse_tag("domain:test")

        big_body = "本文。" * 1500
        over_budget_notes = f"## Big\n{big_body}\n\n## Small\nちいさい\n"
        assert len(over_budget_notes) > _TAG_NOTES_RATCHET_CEILING

        conn = get_connection()
        try:
            conn.execute("DROP TRIGGER trg_tags_notes_ratchet_ceiling_upd")
            conn.execute(
                "UPDATE tags SET notes = ? WHERE namespace = ? AND name = ?",
                (over_budget_notes, namespace, name),
            )
            conn.commit()
            conn.execute(
                """
                CREATE TRIGGER trg_tags_notes_ratchet_ceiling_upd
                BEFORE UPDATE OF notes ON tags
                FOR EACH ROW
                WHEN NEW.notes IS NOT NULL
                     AND LENGTH(NEW.notes) > 4000
                     AND (OLD.notes IS NULL OR LENGTH(NEW.notes) > LENGTH(OLD.notes))
                BEGIN
                    SELECT RAISE(ABORT, 'tag notes ratchet ceiling (4000 chars) exceeded and increasing');
                END;
                """
            )
            conn.commit()
        finally:
            conn.close()

        result = demote_tag_notes("domain:test", sections=["Small"], mode="drop")

        assert "error" not in result
        assert result["notes_length"]["after"] > _TAG_NOTES_RATCHET_CEILING
        assert result["notes_length"]["over_budget"] is True
        assert "ちいさい" not in _get_tag_notes("domain:test")
