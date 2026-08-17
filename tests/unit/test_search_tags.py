"""search_tags機能のユニットテスト"""
import os
import tempfile
import pytest
from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.activity_service import add_activity
from src.services.tag_service import search_tags, _SEARCH_TAGS_RRF_K
import src.services.embedding_service as emb


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)


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


def test_search_tags_basic(temp_db):
    """基本動作: タグ名部分一致で検索できる"""
    add_topic(title="Topic 1", description="Desc", tags=["domain:test"])
    add_topic(title="Topic 2", description="Desc", tags=["domain:testing"])

    result = search_tags("test")
    assert "error" not in result
    assert "tags" in result
    tag_names = [t["name"] for t in result["tags"]]
    assert "test" in tag_names
    assert "testing" in tag_names


def test_search_tags_score_present(temp_db):
    """各タグにscoreフィールドが含まれる"""
    add_topic(title="Topic 1", description="Desc", tags=["domain:test"])

    result = search_tags("test")
    assert "error" not in result
    for tag in result["tags"]:
        assert "score" in tag
        assert isinstance(tag["score"], float)
        assert tag["score"] > 0


def test_search_tags_namespace_filter(temp_db):
    """namespaceフィルタ: 指定namespaceのみ返る"""
    add_topic(title="Topic 1", description="Desc", tags=["domain:test", "intent:design"])

    result = search_tags("test", namespace="domain")
    assert "error" not in result
    for t in result["tags"]:
        assert t["namespace"] == "domain"


def test_search_tags_namespace_filter_intent(temp_db):
    """namespaceフィルタ: intentで絞り込み"""
    add_topic(title="Topic 1", description="Desc", tags=["domain:test", "intent:design"])

    result = search_tags("design", namespace="intent")
    assert "error" not in result
    assert len(result["tags"]) >= 1
    for t in result["tags"]:
        assert t["namespace"] == "intent"


def test_search_tags_namespace_filter_bare(temp_db):
    """namespace=""フィルタ: 素タグのみ返る"""
    add_topic(title="Topic 1", description="Desc", tags=["domain:test", "baretag"])

    result = search_tags("baretag", namespace="")
    assert "error" not in result
    for t in result["tags"]:
        assert t["namespace"] == ""


def test_search_tags_include_notes_false(temp_db):
    """include_notes=False（デフォルト）でnotesが返らない"""
    add_topic(title="Topic 1", description="Desc", tags=["domain:test"])

    result = search_tags("test")
    assert "error" not in result
    for tag in result["tags"]:
        assert "notes" not in tag


def test_search_tags_include_notes_true(temp_db):
    """include_notes=Trueでnotesが返る"""
    add_topic(title="Topic 1", description="Desc", tags=["domain:test"])

    result = search_tags("test", include_notes=True)
    assert "error" not in result
    for tag in result["tags"]:
        assert "notes" in tag


def test_search_tags_limit(temp_db):
    """limitパラメータが機能する"""
    # 複数タグを作成
    for i in range(5):
        add_topic(title=f"Topic {i}", description="Desc", tags=[f"search{i}"])

    result = search_tags("search", limit=3)
    assert "error" not in result
    assert len(result["tags"]) <= 3


def test_search_tags_empty_query(temp_db):
    """空クエリはエラーを返す"""
    result = search_tags("")
    assert "error" in result
    assert result["error"]["code"] == "INVALID_QUERY"


def test_search_tags_whitespace_query(temp_db):
    """空白のみのクエリはエラーを返す"""
    result = search_tags("   ")
    assert "error" in result
    assert result["error"]["code"] == "INVALID_QUERY"


def test_search_tags_no_match(temp_db):
    """マッチしない場合は空配列を返す"""
    add_topic(title="Topic 1", description="Desc", tags=["domain:test"])

    result = search_tags("zzzznonexistent")
    assert "error" not in result
    assert result["tags"] == []


def test_search_tags_response_format(temp_db):
    """レスポンスの各タグのフォーマット確認"""
    add_topic(title="Topic 1", description="Desc", tags=["domain:test"])

    result = search_tags("test")
    assert "error" not in result
    assert len(result["tags"]) >= 1
    tag = result["tags"][0]
    assert "tag" in tag
    assert "id" in tag
    assert "namespace" in tag
    assert "name" in tag
    assert "usage_count" in tag
    assert "score" in tag
    assert "canonical" in tag
    assert "description" in tag
    assert isinstance(tag["id"], int)
    assert isinstance(tag["usage_count"], int)
    assert isinstance(tag["score"], float)


def test_search_tags_description_value(temp_db):
    """descriptionが設定されたタグで値が正しく返る"""
    from src.services.tag_service import update_tag
    add_topic(title="Topic 1", description="Desc", tags=["domain:test"])
    update_tag("domain:test", description="テスト用タグ")

    result = search_tags("test")
    assert "error" not in result
    test_tag = next(t for t in result["tags"] if t["name"] == "test")
    assert test_tag["description"] == "テスト用タグ"


def test_search_tags_canonical(temp_db):
    """エイリアスタグにcanonicalフィールドが含まれる"""
    from src.services.tag_service import update_tag
    add_topic(title="Topic 1", description="Desc", tags=["domain:test", "domain:testing"])
    update_tag("domain:testing", canonical="domain:test")

    result = search_tags("testing", include_notes=True)
    assert "error" not in result
    # testing タグが返ってくる場合、canonicalが設定されているはず
    testing_tag = next((t for t in result["tags"] if t["name"] == "testing"), None)
    if testing_tag:
        assert testing_tag["canonical"] == "domain:test"


def test_search_tags_usage_count(temp_db):
    """usage_countが正しくカウントされる"""
    add_topic(title="Topic 1", description="Desc", tags=["domain:test"])
    add_topic(title="Topic 2", description="Desc", tags=["domain:test"])
    add_activity(title="Activity 1", description="Desc", tags=["domain:test"], check_in=False)

    result = search_tags("test")
    assert "error" not in result
    test_tag = next(t for t in result["tags"] if t["name"] == "test")
    # topic_tags(2) + activity_tags(1) = 3
    assert test_tag["usage_count"] == 3


def test_search_tags_bare_tag(temp_db):
    """素タグも検索できる"""
    add_topic(title="Topic 1", description="Desc", tags=["baretag"])

    result = search_tags("baretag")
    assert "error" not in result
    bare = next((t for t in result["tags"] if t["tag"] == "baretag"), None)
    assert bare is not None
    assert bare["namespace"] == ""
    assert bare["name"] == "baretag"


def test_search_tags_partial_match(temp_db):
    """部分一致で検索できる"""
    add_topic(title="Topic 1", description="Desc", tags=["domain:calm"])

    result = search_tags("cal")
    assert "error" not in result
    tag_names = [t["name"] for t in result["tags"]]
    assert "calm" in tag_names


# ========================================
# RRF統合テスト（search_similar_tags monkeypatch）
# ========================================


def _get_tag_id(name, namespace=""):
    """ヘルパー: タグ名からIDを取得"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM tags WHERE namespace = ? AND name = ?",
            (namespace, name),
        ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


class TestSearchTagsRRF:
    """search_similar_tagsをmonkeypatchしてRRF統合ロジックをテスト"""

    def test_rrf_both_channels_boost_score(self, temp_db, monkeypatch):
        """LIKE・ベクトル両方にヒットするタグはスコアが高くなる"""
        add_topic(title="T1", description="D", tags=["domain:test"])
        add_topic(title="T2", description="D", tags=["domain:other"])

        test_id = _get_tag_id("test", "domain")
        other_id = _get_tag_id("other", "domain")

        # search_similar_tags: testをrank1, otherをrank2で返す
        def mock_search(query, k=10):
            return [(test_id, 0.1), (other_id, 0.2)]

        monkeypatch.setattr(emb, "search_similar_tags", mock_search)

        result = search_tags("test")
        assert "error" not in result
        assert len(result["tags"]) >= 1

        # "test" はLIKEでもvecでもヒット → 両チャネルのRRFスコア合算
        # "other" はvecのみ → vecだけのRRFスコア
        tag_map = {t["name"]: t["score"] for t in result["tags"]}
        assert "test" in tag_map
        if "other" in tag_map:
            assert tag_map["test"] > tag_map["other"]

    def test_rrf_vec_only_tag_appears(self, temp_db, monkeypatch):
        """LIKEに引っかからないがベクトルで近いタグも結果に含まれる"""
        add_topic(title="T1", description="D", tags=["domain:test"])
        add_topic(title="T2", description="D", tags=["domain:unrelated"])

        unrelated_id = _get_tag_id("unrelated", "domain")

        # search_similar_tags: LIKEに引っかからないタグをvecで返す
        def mock_search(query, k=10):
            return [(unrelated_id, 0.05)]

        monkeypatch.setattr(emb, "search_similar_tags", mock_search)

        result = search_tags("test")
        assert "error" not in result
        tag_names = [t["name"] for t in result["tags"]]
        # "unrelated" はLIKE "test" にマッチしないが、vecチャネル経由で登場する
        assert "unrelated" in tag_names

    def test_rrf_namespace_filter_on_vec_results(self, temp_db, monkeypatch):
        """namespaceフィルタがベクトル検索結果にも適用される"""
        add_topic(title="T1", description="D", tags=["domain:test", "intent:design"])

        domain_test_id = _get_tag_id("test", "domain")
        intent_design_id = _get_tag_id("design", "intent")

        # search_similar_tags: 両方返す
        def mock_search(query, k=10):
            return [(domain_test_id, 0.1), (intent_design_id, 0.2)]

        monkeypatch.setattr(emb, "search_similar_tags", mock_search)

        # namespace="domain" で絞り込み
        result = search_tags("test", namespace="domain")
        assert "error" not in result
        for t in result["tags"]:
            assert t["namespace"] == "domain"

    def test_rrf_score_formula(self, temp_db, monkeypatch):
        """RRFスコアが 1/(k+rank) の合算であること"""
        add_topic(title="T1", description="D", tags=["domain:alpha"])

        alpha_id = _get_tag_id("alpha", "domain")

        # search_similar_tags: alphaをrank1で返す
        def mock_search(query, k=10):
            return [(alpha_id, 0.1)]

        monkeypatch.setattr(emb, "search_similar_tags", mock_search)

        result = search_tags("alpha")
        assert "error" not in result
        assert len(result["tags"]) == 1

        tag = result["tags"][0]
        # LIKE rank=1, vec rank=1
        # score = W_LIKE/(K+1) + W_VEC/(K+1) = 2/(60+1)
        expected = round(2.0 / (_SEARCH_TAGS_RRF_K + 1), 4)
        assert tag["score"] == expected

    def test_rrf_limit_applied_after_merge(self, temp_db, monkeypatch):
        """RRF統合後にlimit切り詰めが行われる"""
        # 10個のタグを作成
        for i in range(10):
            add_topic(title=f"T{i}", description="D", tags=[f"item{i}"])

        # search_similar_tagsで全タグをvecチャネルから返す
        tag_ids = []
        for i in range(10):
            tid = _get_tag_id(f"item{i}", "")
            if tid:
                tag_ids.append(tid)

        def mock_search(query, k=10):
            return [(tid, 0.1 * (idx + 1)) for idx, tid in enumerate(tag_ids)]

        monkeypatch.setattr(emb, "search_similar_tags", mock_search)

        result = search_tags("item", limit=3)
        assert "error" not in result
        assert len(result["tags"]) <= 3
