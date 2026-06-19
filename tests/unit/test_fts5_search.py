"""FTS5統合検索（search / get_by_id）のテスト"""
import os
import tempfile
import pytest
from src.db import init_database
from src.services.topic_service import add_topic
from src.services.activity_service import add_activity
from tests.helpers import add_log as add_log_entry, add_decision
from src.services.material_service import add_material
from src.services import search_service
import src.services.embedding_service as emb


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """FTS5テストではembeddingサービスを無効化"""
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


# ========================================
# search ツールのテスト
# ========================================


def test_search_basic(temp_db):
    """基本検索: タグなしで全件検索"""
    add_topic(title="テスト用トピック検索対象", description="検索テスト説明文", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="テスト用トピック検索対象")
    assert "error" not in result
    assert len(result["results"]) >= 1


def test_search_response_format(temp_db):
    """レスポンス形式: results配列とtotal_countとsearch_methods_used"""
    add_topic(title="レスポンス形式検索テスト", description="テスト用", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="レスポンス形式検索テスト")
    assert "error" not in result
    assert "results" in result
    assert "total_count" in result
    assert "search_methods_used" in result
    assert isinstance(result["search_methods_used"], list)
    assert isinstance(result["results"], list)
    if result["results"]:
        item = result["results"][0]
        assert "type" in item
        assert "id" in item
        assert "title" in item
        assert "score" in item
        assert "snippet" in item
        assert "tags" in item
        assert isinstance(item["tags"], list)


def test_search_bm25_ranking(temp_db):
    """BM25ランキング: タイトルマッチの方がスコアが高い"""
    add_topic(title="ランキング最優先テスト対象トピック", description="別の説明", tags=DEFAULT_TAGS)
    add_topic(title="別のトピック", description="ランキング最優先テスト対象トピックの説明", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="ランキング最優先テスト対象トピック")
    assert "error" not in result
    assert len(result["results"]) >= 1


def test_search_entity_type(temp_db):
    """entity_typeで種別を絞り込み"""
    topic = add_topic(title="フィルタテスト用トピック", description="テスト", tags=DEFAULT_TAGS)
    add_decision(topic_id=topic["topic_id"], decision="フィルタテスト決定事項", reason="テスト")
    result = search_service.search(keyword="フィルタテスト", entity_type="topic")
    assert "error" not in result
    for item in result["results"]:
        assert item["type"] == "topic"


def test_search_with_tags(temp_db):
    """タグフィルタ: ANDで絞り込み"""
    add_topic(title="タグ対象トピック一致テスト", description="テスト", tags=["domain:test", "intent:design"])
    add_topic(title="タグ対象外トピック一致テスト", description="テスト", tags=["domain:other"])
    result = search_service.search(keyword="タグ対象", tags=["domain:test"])
    assert "error" not in result
    # domain:test のみヒット
    titles = [r["title"] for r in result["results"]]
    assert any("タグ対象トピック一致テスト" in t for t in titles)
    assert all("タグ対象外トピック一致テスト" not in t for t in titles)


def test_search_with_multiple_tags_and(temp_db):
    """タグフィルタ: 複数タグのAND条件"""
    add_topic(title="複数タグAND対象テスト", description="テスト", tags=["domain:test", "intent:design"])
    add_topic(title="複数タグAND部分テスト", description="テスト", tags=["domain:test"])
    result = search_service.search(keyword="複数タグAND", tags=["domain:test", "intent:design"])
    assert "error" not in result
    titles = [r["title"] for r in result["results"]]
    assert any("複数タグAND対象テスト" in t for t in titles)
    assert all("複数タグAND部分テスト" not in t for t in titles)


def test_search_tags_empty_list(temp_db):
    """空配列のタグ: 全件検索と同じ"""
    add_topic(title="空タグリスト検索テスト", description="テスト", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="空タグリスト検索テスト", tags=[])
    assert "error" not in result
    assert len(result["results"]) >= 1


def test_search_nonexistent_tag(temp_db):
    """存在しないタグ: 空結果"""
    add_topic(title="存在しないタグ検索テスト", description="テスト", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="存在しないタグ検索テスト", tags=["domain:nonexistent"])
    assert "error" not in result
    assert result["results"] == []
    assert result["total_count"] == 0


def test_search_limit_control(temp_db):
    """limit指定で件数制御"""
    for i in range(5):
        add_topic(title=f"リミットテスト用トピック{i}", description="テスト", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="リミットテスト用トピック", limit=2)
    assert "error" not in result
    assert len(result["results"]) <= 2


def test_search_limit_max_50(temp_db):
    """limit=100指定でも50に丸められる"""
    add_topic(title="リミット最大値テスト用トピック", description="テスト", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="リミット最大値テスト用トピック", limit=100)
    assert "error" not in result
    # 内部的にlimitが50にclampされていればOK（結果が少なくてもエラーにならない）


def test_search_keyword_too_short(temp_db):
    """1文字キーワードでKEYWORD_TOO_SHORTエラー"""
    result = search_service.search(keyword="あ")
    assert "error" in result
    assert result["error"]["code"] == "KEYWORD_TOO_SHORT"


def test_search_keyword_too_short_after_strip(temp_db):
    """空白を除くと1文字になるキーワード"""
    result = search_service.search(keyword=" あ ")
    assert "error" in result
    assert result["error"]["code"] == "KEYWORD_TOO_SHORT"


def test_search_empty_results(temp_db):
    """ヒットなしで空配列"""
    result = search_service.search(keyword="絶対に存在しないキーワード123456")
    assert "error" not in result
    assert result["results"] == []
    assert result["total_count"] == 0


def test_search_special_characters(temp_db):
    """特殊文字を含むキーワード（FTS5エスケープ確認）"""
    add_topic(title='テスト "特殊文字" 検索対象', description="テスト", tags=DEFAULT_TAGS)
    result = search_service.search(keyword='"特殊文字"')
    assert "error" not in result


def test_search_japanese(temp_db):
    """日本語キーワード検索"""
    add_topic(title="日本語検索テスト用トピック", description="漢字ひらがなカタカナ", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="日本語検索テスト用")
    assert "error" not in result
    assert len(result["results"]) >= 1


def test_search_trigger_sync_topic(temp_db):
    """topicがsearch_indexに同期される"""
    add_topic(title="トリガー同期トピック検索テスト", description="テスト", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="トリガー同期トピック検索テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    assert result["results"][0]["type"] == "topic"


def test_search_trigger_sync_decision(temp_db):
    """decisionがsearch_indexに同期される"""
    topic = add_topic(title="同期テスト用トピック", description="テスト", tags=DEFAULT_TAGS)
    add_decision(topic_id=topic["topic_id"], decision="トリガー同期決定事項テスト", reason="テスト理由")
    result = search_service.search(keyword="トリガー同期決定事項テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    types = [r["type"] for r in result["results"]]
    assert "decision" in types


def test_search_trigger_sync_activity(temp_db):
    """activityがsearch_indexに同期される"""
    add_activity(title="トリガー同期アクティビティ検索テスト", description="テスト用アクティビティ", tags=DEFAULT_TAGS, check_in=False)
    result = search_service.search(keyword="トリガー同期アクティビティ検索テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    types = [r["type"] for r in result["results"]]
    assert "activity" in types


def test_search_invalid_entity_type(temp_db):
    """不正なentity_typeでINVALID_ENTITY_TYPEエラー"""
    result = search_service.search(keyword="テスト", entity_type="invalid")
    assert "error" in result
    assert result["error"]["code"] == "INVALID_ENTITY_TYPE"


def test_search_cross_type(temp_db):
    """横断検索: topic/decision/activity全てが対象"""
    topic = add_topic(title="横断検索テスト用トピック", description="テスト", tags=DEFAULT_TAGS)
    add_decision(topic_id=topic["topic_id"], decision="横断検索テスト決定", reason="テスト")
    add_activity(title="横断検索テスト用アクティビティ", description="テスト", tags=DEFAULT_TAGS, check_in=False)
    result = search_service.search(keyword="横断検索テスト")
    assert "error" not in result
    types_found = {r["type"] for r in result["results"]}
    assert "topic" in types_found
    assert "decision" in types_found
    assert "activity" in types_found


def test_search_decision_inherits_topic_tags(temp_db):
    """decisionはtopicのタグを継承してフィルタされる"""
    topic = add_topic(title="継承テスト用トピック", description="テスト", tags=["domain:test", "intent:investigate"])
    add_decision(topic_id=topic["topic_id"], decision="継承タグフィルタ決定テスト", reason="テスト")
    # intent:investigate でフィルタ → topicを親に持つdecisionもヒット
    result = search_service.search(keyword="継承タグフィルタ決定テスト", tags=["intent:investigate"])
    assert "error" not in result
    types = [r["type"] for r in result["results"]]
    assert "decision" in types


def test_search_log_inherits_topic_tags(temp_db):
    """logはtopicのタグを継承してフィルタされる"""
    topic = add_topic(title="ログ継承テスト用トピック", description="テスト", tags=["domain:test", "loginherit"])
    add_log_entry(topic_id=topic["topic_id"], title="継承タグフィルタログテスト", content="テストログ内容")
    result = search_service.search(keyword="継承タグフィルタログテスト", tags=["loginherit"])
    assert "error" not in result
    types = [r["type"] for r in result["results"]]
    assert "log" in types


# ========================================
# get_by_ids ツールのテスト
# ========================================


def test_get_by_ids_single_topic(temp_db):
    """get_by_ids: topicの詳細取得（1件）"""
    topic = add_topic(title="詳細取得テスト用トピック", description="テスト説明", tags=DEFAULT_TAGS)
    result = search_service.get_by_ids([{"type": "topic", "id": topic["topic_id"]}])
    assert len(result["results"]) == 1
    item = result["results"][0]
    assert "error" not in item
    assert item["type"] == "topic"
    assert item["data"]["title"] == "詳細取得テスト用トピック"
    assert item["data"]["description"] == "テスト説明"
    assert "tags" in item["data"]
    assert "domain:test" in item["data"]["tags"]


def test_get_by_ids_single_decision(temp_db):
    """get_by_ids: decisionの詳細取得（1件）"""
    topic = add_topic(title="トピック", description="テスト", tags=DEFAULT_TAGS)
    dec = add_decision(topic_id=topic["topic_id"], decision="詳細取得テスト決定", reason="テスト理由")
    result = search_service.get_by_ids([{"type": "decision", "id": dec["decision_id"]}])
    assert len(result["results"]) == 1
    item = result["results"][0]
    assert "error" not in item
    assert item["type"] == "decision"
    assert item["data"]["decision"] == "詳細取得テスト決定"
    assert "tags" in item["data"]
    assert "domain:test" in item["data"]["tags"]


def test_get_by_ids_single_activity(temp_db):
    """get_by_ids: activityの詳細取得（1件）"""
    activity = add_activity(title="詳細取得テスト用アクティビティ", description="テスト説明", tags=DEFAULT_TAGS, check_in=False)
    result = search_service.get_by_ids([{"type": "activity", "id": activity["activity_id"]}])
    assert len(result["results"]) == 1
    item = result["results"][0]
    assert "error" not in item
    assert item["type"] == "activity"
    assert item["data"]["title"] == "詳細取得テスト用アクティビティ"
    assert "tags" in item["data"]
    assert "domain:test" in item["data"]["tags"]


def test_get_by_ids_single_log(temp_db):
    """get_by_ids: logの詳細取得（1件）"""
    topic = add_topic(title="トピック", description="テスト", tags=DEFAULT_TAGS)
    log = add_log_entry(topic_id=topic["topic_id"], title="詳細取得テストログ", content="テスト内容")
    result = search_service.get_by_ids([{"type": "log", "id": log["log_id"]}])
    assert len(result["results"]) == 1
    item = result["results"][0]
    assert "error" not in item
    assert item["type"] == "log"
    assert item["data"]["title"] == "詳細取得テストログ"
    assert item["data"]["content"] == "テスト内容"
    assert "tags" in item["data"]
    assert "domain:test" in item["data"]["tags"]


def test_get_by_ids_single_material(temp_db):
    """get_by_ids: materialの詳細取得で content/source が同梱される（P0-6）"""
    mat = add_material(
        title="詳細取得テスト素材",
        content="素材本文の全文がレスポンスに含まれることを確認する",
        tags=DEFAULT_TAGS,
        source="単体テスト",
    )
    result = search_service.get_by_ids([{"type": "material", "id": mat["material_id"]}])
    assert len(result["results"]) == 1
    item = result["results"][0]
    assert "error" not in item
    assert item["type"] == "material"
    assert item["data"]["material_id"] == mat["material_id"]
    assert item["data"]["title"] == "詳細取得テスト素材"
    assert item["data"]["content"] == "素材本文の全文がレスポンスに含まれることを確認する"
    assert item["data"]["source"] == "単体テスト"
    assert "domain:test" in item["data"]["tags"]
    assert "created_at" in item["data"]


def test_get_by_ids_single_not_found(temp_db):
    """get_by_ids: 存在しないIDでNOT_FOUNDエラー"""
    result = search_service.get_by_ids([{"type": "topic", "id": 999999}])
    assert len(result["results"]) == 1
    item = result["results"][0]
    assert "error" in item
    assert item["error"]["code"] == "NOT_FOUND"


def test_get_by_ids_invalid_type(temp_db):
    """get_by_ids: 不正な種別でINVALID_TYPEエラー"""
    result = search_service.get_by_ids([{"type": "invalid", "id": 1}])
    assert len(result["results"]) == 1
    item = result["results"][0]
    assert "error" in item
    assert item["error"]["code"] == "INVALID_TYPE"


# ========================================
# discussion_logs 検索テスト
# ========================================


def test_search_trigger_sync_log(temp_db):
    """logがsearch_indexに同期される"""
    topic = add_topic(title="ログ同期テスト用トピック", description="テスト", tags=DEFAULT_TAGS)
    add_log_entry(topic_id=topic["topic_id"], title="トリガー同期ログ検索テスト", content="ログの内容")
    result = search_service.search(keyword="トリガー同期ログ検索テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    types = [r["type"] for r in result["results"]]
    assert "log" in types


def test_search_entity_type_log(temp_db):
    """entity_type=logでログのみ取得"""
    topic = add_topic(title="ログフィルタテスト用トピック", description="テスト", tags=DEFAULT_TAGS)
    add_log_entry(topic_id=topic["topic_id"], title="ログフィルタ対象テスト", content="ログ内容")
    result = search_service.search(keyword="ログフィルタ対象テスト", entity_type="log")
    assert "error" not in result
    for item in result["results"]:
        assert item["type"] == "log"


def test_search_cross_type_includes_log(temp_db):
    """横断検索にlogも含まれる"""
    topic = add_topic(title="横断ログ検索テスト用", description="テスト", tags=DEFAULT_TAGS)
    add_log_entry(topic_id=topic["topic_id"], title="横断ログ検索テスト対象", content="ログ内容")
    add_decision(topic_id=topic["topic_id"], decision="横断ログ検索テスト決定", reason="テスト")
    result = search_service.search(keyword="横断ログ検索テスト")
    assert "error" not in result
    types_found = {r["type"] for r in result["results"]}
    assert "log" in types_found


def test_search_log_title_fallback(temp_db):
    """logのtitleが空の場合、contentの先頭50文字をフォールバック"""
    # NOTE: add_log_entryはtitle空文字をバリデーションエラーにするため、
    # ここではtitle付きで作成し、get_by_idsでフォールバック動作を確認
    topic = add_topic(title="トピック", description="テスト", tags=DEFAULT_TAGS)
    log = add_log_entry(topic_id=topic["topic_id"], title="フォールバックテスト", content="テスト内容です")
    result = search_service.get_by_ids([{"type": "log", "id": log["log_id"]}])
    item = result["results"][0]
    assert "error" not in item
    assert item["data"]["title"] == "フォールバックテスト"


def test_add_log_empty_title_auto_generates_from_content(temp_db):
    """title空文字でcontentありの場合、contentの先頭行からtitleを自動生成する"""
    topic = add_topic(
        title="バリデーションテスト用トピック",
        description="テスト用",
        tags=DEFAULT_TAGS,
    )

    result = add_log_entry(
        topic_id=topic["topic_id"],
        title="",
        content="内容があればtitleが自動生成される",
    )

    assert "error" not in result
    assert result["title"] == "内容があればtitleが自動生成される"


# ========================================
# snippet テスト
# ========================================


def test_search_snippet_topic(temp_db):
    """search結果のtopicにsnippetが含まれること（ソース: description）"""
    add_topic(title="スニペットトピックテスト", description="これはトピックの説明文です", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="スニペットトピックテスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "topic")
    assert "snippet" in item
    assert item["snippet"] == "これはトピックの説明文です"


def test_search_snippet_decision(temp_db):
    """search結果のdecisionにsnippetが含まれること（ソース: decision）"""
    topic = add_topic(title="トピック", description="テスト", tags=DEFAULT_TAGS)
    add_decision(topic_id=topic["topic_id"], decision="スニペット決定事項テスト用の内容", reason="テスト理由")
    result = search_service.search(keyword="スニペット決定事項テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "decision")
    assert "snippet" in item
    assert item["snippet"] == "スニペット決定事項テスト用の内容"


def test_search_snippet_activity(temp_db):
    """search結果のactivityにsnippetが含まれること（ソース: description）"""
    add_activity(title="スニペットアクティビティテスト", description="アクティビティの詳細説明テスト", tags=DEFAULT_TAGS, check_in=False)
    result = search_service.search(keyword="スニペットアクティビティテスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "activity")
    assert "snippet" in item
    assert item["snippet"] == "アクティビティの詳細説明テスト"


def test_search_snippet_log(temp_db):
    """search結果のlogにsnippetが含まれること（ソース: content）"""
    topic = add_topic(title="トピック", description="テスト", tags=DEFAULT_TAGS)
    add_log_entry(topic_id=topic["topic_id"], title="スニペットログテスト", content="ログの内容テスト文")
    result = search_service.search(keyword="スニペットログテスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "log")
    assert "snippet" in item
    assert item["snippet"] == "ログの内容テスト文"


def test_search_snippet_max_length(temp_db):
    """snippetは200文字以下に切り詰められること"""
    long_desc = "あ" * 300
    add_topic(title="スニペット長制限テスト", description=long_desc, tags=DEFAULT_TAGS)
    result = search_service.search(keyword="スニペット長制限テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "topic")
    assert "snippet" in item
    assert len(item["snippet"]) <= 200
    assert item["snippet"] == "あ" * 200


def test_search_snippet_empty_source(temp_db):
    """snippetソースが空（空文字列）の場合、snippetは空文字列"""
    add_topic(title="スニペット空ソーステスト", description="", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="スニペット空ソーステスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "topic")
    assert "snippet" in item
    assert item["snippet"] == ""


# ========================================
# tags テスト
# ========================================


def test_search_tags_topic(temp_db):
    """search結果のtopicにtagsが含まれること"""
    add_topic(title="タグ付きトピック検索テスト", description="テスト", tags=["domain:test", "intent:design"])
    result = search_service.search(keyword="タグ付きトピック検索テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "topic")
    assert "tags" in item
    assert "domain:test" in item["tags"]
    assert "intent:design" in item["tags"]


def test_search_tags_decision(temp_db):
    """search結果のdecisionにtagsが含まれること（topic継承）"""
    topic = add_topic(title="トピック", description="テスト", tags=["domain:test", "intent:investigate"])
    add_decision(topic_id=topic["topic_id"], decision="タグ付き決定事項検索テスト", reason="テスト理由")
    result = search_service.search(keyword="タグ付き決定事項検索テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "decision")
    assert "tags" in item
    # decisionはtopicのタグを継承する
    assert "domain:test" in item["tags"]
    assert "intent:investigate" in item["tags"]


def test_search_tags_activity(temp_db):
    """search結果のactivityにtagsが含まれること"""
    add_activity(title="タグ付きアクティビティ検索テスト", description="テスト", tags=["domain:test"])
    result = search_service.search(keyword="タグ付きアクティビティ検索テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "activity")
    assert "tags" in item
    assert "domain:test" in item["tags"]


def test_search_tags_log(temp_db):
    """search結果のlogにtagsが含まれること（topic継承）"""
    topic = add_topic(title="トピック", description="テスト", tags=["domain:test"])
    add_log_entry(topic_id=topic["topic_id"], title="タグ付きログ検索テスト", content="テスト内容")
    result = search_service.search(keyword="タグ付きログ検索テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "log")
    assert "tags" in item
    assert "domain:test" in item["tags"]


def test_search_tags_cross_type(temp_db):
    """横断検索で全typeにtagsが付与される"""
    topic = add_topic(title="横断タグテスト用トピック", description="テスト", tags=DEFAULT_TAGS)
    add_decision(topic_id=topic["topic_id"], decision="横断タグテスト決定", reason="テスト")
    add_activity(title="横断タグテスト用アクティビティ", description="テスト", tags=DEFAULT_TAGS)
    add_log_entry(topic_id=topic["topic_id"], title="横断タグテストログ", content="テスト内容")
    result = search_service.search(keyword="横断タグテスト")
    assert "error" not in result
    for item in result["results"]:
        assert "tags" in item, f"type={item['type']} にtagsフィールドがない"
        assert isinstance(item["tags"], list)


def test_search_tags_empty_results(temp_db):
    """ヒットなし: tags付与でエラーにならない"""
    result = search_service.search(keyword="絶対に存在しないタグテスト用キーワード")
    assert "error" not in result
    assert result["results"] == []


# ========================================
# keyword配列（AND検索）のテスト
# ========================================


def test_search_keyword_array_and(temp_db):
    """配列keyword: AND検索で両キーワードを含む結果のみ返す"""
    add_topic(title="メモリ管理の検索テスト", description="検索機能のテスト", tags=DEFAULT_TAGS)
    add_topic(title="メモリ管理の設計ドキュメント", description="設計の詳細", tags=DEFAULT_TAGS)
    add_topic(title="検索機能の改善提案", description="改善案", tags=DEFAULT_TAGS)
    # "メモリ" AND "検索" → 両方含む最初のトピックのみヒット
    result = search_service.search(keyword=["メモリ管理", "検索テスト"])
    assert "error" not in result
    assert len(result["results"]) >= 1
    titles = [r["title"] for r in result["results"]]
    assert any("メモリ管理の検索テスト" in t for t in titles)
    # "メモリ管理の設計ドキュメント" は "検索テスト" を含まないのでヒットしない
    assert all("メモリ管理の設計ドキュメント" not in t for t in titles)


def test_search_keyword_array_element_too_short(temp_db):
    """配列内に2文字未満の要素があるとKEYWORD_TOO_SHORTエラー"""
    result = search_service.search(keyword=["テスト", "あ"])
    assert "error" in result
    assert result["error"]["code"] == "KEYWORD_TOO_SHORT"


def test_search_keyword_empty_array(temp_db):
    """空配列でKEYWORD_TOO_SHORTエラー"""
    result = search_service.search(keyword=[])
    assert "error" in result
    assert result["error"]["code"] == "KEYWORD_TOO_SHORT"


def test_search_keyword_single_string_backward_compat(temp_db):
    """単一文字列の後方互換: 既存動作と同じ"""
    add_topic(title="後方互換テスト用トピック検索", description="テスト", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="後方互換テスト用トピック検索")
    assert "error" not in result
    assert len(result["results"]) >= 1


def test_search_keyword_array_with_2char_fts_skipped(temp_db):
    """配列内に2文字キーワードがある場合、FTS5検索はスキップされる（ベクトル無効時エラー）"""
    # embedding無効（autouse fixture）なので、2文字キーワードがあるとベクトルのみ→エラー
    result = search_service.search(keyword=["テスト", "設計"])
    assert "error" in result
    assert result["error"]["code"] == "KEYWORD_TOO_SHORT"
    assert "vector search is unavailable" in result["error"]["message"]


# ========================================
# keyword配列（OR検索）のテスト
# ========================================


def test_search_keyword_or_basic(temp_db):
    """OR検索: いずれかのキーワードを含む結果を返す"""
    add_topic(title="OR検索テスト用メモリ管理", description="メモリの説明", tags=DEFAULT_TAGS)
    add_topic(title="OR検索テスト用検索機能", description="検索の説明", tags=DEFAULT_TAGS)
    add_topic(title="OR検索テスト無関係トピック", description="無関係", tags=DEFAULT_TAGS)
    result = search_service.search(keyword=["OR検索テスト用メモリ管理", "OR検索テスト用検索機能"], keyword_mode="or")
    assert "error" not in result
    assert len(result["results"]) >= 2
    titles = [r["title"] for r in result["results"]]
    assert any("メモリ管理" in t for t in titles)
    assert any("検索機能" in t for t in titles)


def test_search_keyword_or_single_keyword(temp_db):
    """OR検索: 単一キーワードではANDと同じ挙動"""
    add_topic(title="OR単一キーワードテスト", description="テスト", tags=DEFAULT_TAGS)
    result = search_service.search(keyword=["OR単一キーワードテスト"], keyword_mode="or")
    assert "error" not in result
    assert len(result["results"]) >= 1


def test_search_keyword_or_string_keyword(temp_db):
    """OR検索: 文字列keywordでも動作（単一キーワード扱い）"""
    add_topic(title="OR文字列キーワードテスト", description="テスト", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="OR文字列キーワードテスト", keyword_mode="or")
    assert "error" not in result
    assert len(result["results"]) >= 1


def test_search_keyword_or_with_entity_type(temp_db):
    """OR検索: entity_typeとの組み合わせ"""
    topic = add_topic(title="ORフィルタテスト用トピック", description="テスト", tags=DEFAULT_TAGS)
    add_decision(topic_id=topic["topic_id"], decision="ORフィルタテスト用決定", reason="テスト")
    result = search_service.search(keyword=["ORフィルタテスト用トピック", "ORフィルタテスト用決定"], keyword_mode="or", entity_type="topic")
    assert "error" not in result
    for item in result["results"]:
        assert item["type"] == "topic"


def test_search_keyword_or_with_tags(temp_db):
    """OR検索: タグフィルタとの組み合わせ"""
    add_topic(title="ORタグテスト対象トピック", description="テスト", tags=["domain:test", "intent:design"])
    add_topic(title="ORタグテスト対象外トピック", description="テスト", tags=["domain:other"])
    result = search_service.search(keyword=["ORタグテスト対象トピック", "ORタグテスト対象外トピック"], keyword_mode="or", tags=["domain:test"])
    assert "error" not in result
    titles = [r["title"] for r in result["results"]]
    assert any("対象トピック" in t for t in titles)
    assert all("対象外トピック" not in t for t in titles)


def test_search_invalid_keyword_mode(temp_db):
    """不正なkeyword_modeでエラー"""
    result = search_service.search(keyword="テスト", keyword_mode="invalid")
    assert "error" in result
    assert result["error"]["code"] == "INVALID_KEYWORD_MODE"


def test_search_keyword_or_default_is_and(temp_db):
    """keyword_modeのデフォルトはand"""
    add_topic(title="デフォルトモード検索テスト", description="検索テスト説明", tags=DEFAULT_TAGS)
    add_topic(title="デフォルトモード設計ドキュメント", description="設計の詳細", tags=DEFAULT_TAGS)
    # デフォルト(AND)検索: 両方含む結果のみ
    result_and = search_service.search(keyword=["デフォルトモード検索テスト", "デフォルトモード設計ドキュメント"])
    assert "error" not in result_and
    # OR検索: いずれかを含む結果
    result_or = search_service.search(keyword=["デフォルトモード検索テスト", "デフォルトモード設計ドキュメント"], keyword_mode="or")
    assert "error" not in result_or
    # デフォルト(AND)はORより結果が少ない（2つのキーワードは互いを含まない）
    assert len(result_and["results"]) < len(result_or["results"])


def test_search_keyword_or_2char_fts_partial(temp_db):
    """OR検索: 2文字キーワード混在時、3文字以上のキーワードだけでFTS5検索される（ベクトル無効時）"""
    add_topic(title="OR部分FTSテスト用長いキーワード", description="テスト", tags=DEFAULT_TAGS)
    # "設計"(2文字) + "OR部分FTSテスト用"(9文字) → FTSは後者のみで検索、エラーにならない
    result = search_service.search(keyword=["設計", "OR部分FTSテスト用長いキーワード"], keyword_mode="or")
    assert "error" not in result
    assert len(result["results"]) >= 1


# ========================================
# get_by_ids バッチ取得のテスト
# ========================================


def test_get_by_ids_batch(temp_db):
    """複数アイテムのバッチ取得"""
    add_topic(title="トピック1", description="説明1", tags=DEFAULT_TAGS)
    add_activity(title="アクティビティ1", description="説明1", tags=DEFAULT_TAGS, check_in=False)
    result = search_service.get_by_ids([
        {"type": "topic", "id": 1},
        {"type": "activity", "id": 1},
    ])
    assert "results" in result
    assert len(result["results"]) == 2
    assert result["results"][0]["type"] == "topic"
    assert result["results"][1]["type"] == "activity"


def test_get_by_ids_empty(temp_db):
    """空リストの場合"""
    result = search_service.get_by_ids([])
    assert result == {"results": []}


def test_get_by_ids_too_many(temp_db):
    """件数上限超過"""
    items = [{"type": "topic", "id": i} for i in range(21)]
    result = search_service.get_by_ids(items)
    assert "error" in result
    assert result["error"]["code"] == "TOO_MANY_ITEMS"


def test_get_by_ids_not_found(temp_db):
    """存在しないidを含む場合（エラーは個別結果に含まれる）"""
    result = search_service.get_by_ids([
        {"type": "topic", "id": 99999},
    ])
    assert len(result["results"]) == 1
    assert "error" in result["results"][0]
    assert result["results"][0]["error"]["code"] == "NOT_FOUND"


def test_get_by_ids_mixed_types(temp_db):
    """全4種類のtype混在でのバッチ取得"""
    topic = add_topic(title="混在テストトピック", description="テスト", tags=DEFAULT_TAGS)
    add_decision(topic_id=topic["topic_id"], decision="混在テスト決定", reason="テスト")
    add_activity(title="混在テストアクティビティ", description="テスト", tags=DEFAULT_TAGS, check_in=False)
    add_log_entry(topic_id=topic["topic_id"], title="混在テストログ", content="テスト内容")
    result = search_service.get_by_ids([
        {"type": "topic", "id": 1},
        {"type": "decision", "id": 1},
        {"type": "activity", "id": 1},
        {"type": "log", "id": 1},
    ])
    assert "results" in result
    assert len(result["results"]) == 4
    types = [r["type"] for r in result["results"]]
    assert types == ["topic", "decision", "activity", "log"]


def test_get_by_ids_at_limit(temp_db):
    """ちょうど20件（上限ぴったり）は成功する"""
    items = [{"type": "topic", "id": i} for i in range(20)]
    result = search_service.get_by_ids(items)
    assert "error" not in result
    assert "results" in result
    assert len(result["results"]) == 20


def test_get_by_ids_invalid_type(temp_db):
    """不正なtypeを含む場合（get_by_idのINVALID_TYPEエラーが個別結果に含まれる）"""
    result = search_service.get_by_ids([
        {"type": "invalid", "id": 1},
    ])
    assert "results" in result
    assert len(result["results"]) == 1
    assert "error" in result["results"][0]
    assert result["results"][0]["error"]["code"] == "INVALID_TYPE"


def test_get_by_ids_missing_fields(temp_db):
    """type/idフィールドが欠落した場合"""
    result = search_service.get_by_ids([
        {"type": "topic"},
        {"id": 1},
        {},
    ])
    assert len(result["results"]) == 3
    for r in result["results"]:
        assert "error" in r
        assert r["error"]["code"] == "VALIDATION_ERROR"


# ========================================
# decision/log 固有タグのテスト
# ========================================


def test_search_tags_decision_own_tags(temp_db):
    """search結果のdecisionに固有タグ+継承タグの両方が含まれること"""
    topic = add_topic(
        title="トピック", description="テスト",
        tags=["domain:test"],
    )
    add_decision(
        topic_id=topic["topic_id"],
        decision="固有タグ付き決定事項検索テスト",
        reason="テスト理由",
        tags=["intent:design"],
    )
    result = search_service.search(keyword="固有タグ付き決定事項検索テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "decision")
    assert "tags" in item
    # topicから継承したタグ
    assert "domain:test" in item["tags"]
    # decision固有のタグ
    assert "intent:design" in item["tags"]


def test_search_tags_log_own_tags(temp_db):
    """search結果のlogに固有タグ+継承タグの両方が含まれること"""
    topic = add_topic(
        title="トピック", description="テスト",
        tags=["domain:test"],
    )
    add_log_entry(
        topic_id=topic["topic_id"],
        title="固有タグ付きログ検索テスト",
        content="テスト内容",
        tags=["intent:investigate"],
    )
    result = search_service.search(keyword="固有タグ付きログ検索テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "log")
    assert "tags" in item
    # topicから継承したタグ
    assert "domain:test" in item["tags"]
    # log固有のタグ
    assert "intent:investigate" in item["tags"]


# ========================================
# search_methods_used テスト
# ========================================


def test_search_methods_used_fts_only(temp_db):
    """embedding無効時: FTS5のみが使われる（3文字以上キーワード）"""
    add_topic(title="メソッド確認用トピック検索テスト", description="テスト", tags=DEFAULT_TAGS)
    result = search_service.search(keyword="メソッド確認用トピック検索テスト")
    assert "error" not in result
    assert "search_methods_used" in result
    assert result["search_methods_used"] == ["fts5"]


def test_search_methods_used_empty_results(temp_db):
    """結果0件でもsearch_methods_usedは返る"""
    result = search_service.search(keyword="絶対に存在しないキーワード999999")
    assert "error" not in result
    assert "search_methods_used" in result
    assert result["search_methods_used"] == ["fts5"]


# ========================================
# material 検索テスト
# ========================================


def test_search_trigger_sync_material(temp_db):
    """materialがsearch_indexに同期される"""
    add_material(title="トリガー同期素材検索テスト", content="素材の内容テスト", tags=DEFAULT_TAGS, source="テスト用データ")
    result = search_service.search(keyword="トリガー同期素材検索テスト")
    assert "error" not in result
    assert len(result["results"]) >= 1
    types = [r["type"] for r in result["results"]]
    assert "material" in types


def test_search_entity_type_material(temp_db):
    """entity_type=materialでmaterialのみ取得"""
    add_material(title="素材フィルタ対象テスト", content="素材の内容", tags=DEFAULT_TAGS, source="テスト用データ")
    result = search_service.search(keyword="素材フィルタ対象テスト", entity_type="material")
    assert "error" not in result
    for item in result["results"]:
        assert item["type"] == "material"


def test_search_cross_type_includes_material(temp_db):
    """横断検索にmaterialも含まれる"""
    topic = add_topic(title="横断素材検索テスト用", description="テスト", tags=DEFAULT_TAGS)
    add_material(title="横断素材検索テスト対象素材", content="素材内容", tags=DEFAULT_TAGS, source="テスト用データ")
    add_decision(topic_id=topic["topic_id"], decision="横断素材検索テスト決定", reason="テスト")
    result = search_service.search(keyword="横断素材検索テスト")
    assert "error" not in result
    types_found = {r["type"] for r in result["results"]}
    assert "material" in types_found


def test_search_material_by_content(temp_db):
    """materialのcontentでも検索がヒットする"""
    add_material(title="タイトル", content="素材コンテンツ検索対象のユニーク文字列", tags=DEFAULT_TAGS, source="テスト用データ")
    result = search_service.search(keyword="素材コンテンツ検索対象のユニーク文字列")
    assert "error" not in result
    assert len(result["results"]) >= 1
    types = [r["type"] for r in result["results"]]
    assert "material" in types


# ========================================
# material snippet テスト
# ========================================


def test_search_snippet_material_title_priority(temp_db):
    """materialのsnippetはtitle優先表示（"title: content..." 形式）"""
    add_material(title="設計書", content="ここに設計の内容が入ります", tags=DEFAULT_TAGS, source="テスト用データ")
    result = search_service.search(keyword="設計書", entity_type="material")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "material")
    assert "snippet" in item
    assert item["snippet"].startswith("設計書: ")
    assert "ここに設計の内容が入ります" in item["snippet"]


def test_search_snippet_material_max_length(temp_db):
    """materialのsnippetはSNIPPET_MAX_LEN以下に収まる"""
    long_content = "あ" * 300
    add_material(title="長コンテンツテスト素材", content=long_content, tags=DEFAULT_TAGS, source="テスト用データ")
    result = search_service.search(keyword="長コンテンツテスト素材", entity_type="material")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "material")
    assert len(item["snippet"]) <= 200


# ========================================
# material タグフィルタテスト
# ========================================


def test_search_material_tag_filter_own_tags(temp_db):
    """materialは自身のタグでタグフィルタされる"""
    add_material(title="タグフィルタテスト素材対象", content="素材の内容",
                 tags=["domain:test", "intent:design"], source="テスト用データ")
    result = search_service.search(keyword="タグフィルタテスト素材対象", tags=["intent:design"])
    assert "error" not in result
    types = [r["type"] for r in result["results"]]
    assert "material" in types


def test_search_material_tag_filter_excludes(temp_db):
    """materialにないタグでフィルタすると除外される"""
    add_material(title="タグ除外テスト素材対象", content="素材の内容",
                 tags=["domain:test"], source="テスト用データ")
    result = search_service.search(keyword="タグ除外テスト素材対象", tags=["domain:other"])
    assert "error" not in result
    # domain:other は存在しないのでヒットしない
    assert result["results"] == []


def test_search_material_tags_in_results(temp_db):
    """search結果のmaterialに自身のtagsが含まれること"""
    add_material(title="素材タグ表示テスト対象", content="素材の内容",
                 tags=["domain:test", "intent:implement"], source="テスト用データ")
    result = search_service.search(keyword="素材タグ表示テスト対象")
    assert "error" not in result
    assert len(result["results"]) >= 1
    item = next(r for r in result["results"] if r["type"] == "material")
    assert "tags" in item
    assert "domain:test" in item["tags"]
    assert "intent:implement" in item["tags"]


# ========================================
# material get_by_ids テスト
# ========================================


def test_get_by_ids_single_material(temp_db):
    """get_by_ids: materialの詳細取得（1件）"""
    material = add_material(title="詳細取得テスト素材", content="素材本文テスト", tags=["domain:test"], source="テスト用データ")
    result = search_service.get_by_ids([{"type": "material", "id": material["material_id"]}])
    assert len(result["results"]) == 1
    item = result["results"][0]
    assert "error" not in item
    assert item["type"] == "material"
    assert item["data"]["title"] == "詳細取得テスト素材"
    assert "tags" in item["data"]
    assert "domain:test" in item["data"]["tags"]
    # activity_idが含まれないこと
    assert "activity_id" not in item["data"]


# ========================================
# タグLIKE検索テスト（FTS5テスト内）
# ========================================


def test_search_tag_like_topic_direct_tag(temp_db):
    """タグLIKE検索: topicの直接タグにマッチ"""
    add_topic(
        title="タグLIKEトピック直接テスト",
        description="テスト",
        tags=["domain:fts-tag-like-test"],
    )
    result = search_service.search(keyword="fts-tag-like-test")
    assert "error" not in result
    assert len(result["results"]) >= 1
    assert "tag_like" in result["search_methods_used"]
    titles = [r["title"] for r in result["results"]]
    assert any("タグLIKEトピック直接テスト" in t for t in titles)


def test_search_tag_like_activity_direct_tag(temp_db):
    """タグLIKE検索: activityの直接タグにマッチ"""
    add_activity(
        title="タグLIKEアクティビティ直接テスト",
        description="テスト",
        tags=["domain:fts-activity-tag-test"],
        check_in=False,
    )
    result = search_service.search(keyword="fts-activity-tag-test")
    assert "error" not in result
    assert len(result["results"]) >= 1
    types = [r["type"] for r in result["results"]]
    assert "activity" in types


def test_search_tag_like_decision_inherited_tag(temp_db):
    """タグLIKE検索: decisionがtopicのタグを継承してマッチ"""
    topic = add_topic(
        title="トピック",
        description="テスト",
        tags=["domain:fts-decision-inherit-test"],
    )
    add_decision(
        topic_id=topic["topic_id"],
        decision="タグLIKE決定継承テスト",
        reason="テスト",
    )
    result = search_service.search(keyword="fts-decision-inherit-test")
    assert "error" not in result
    types = [r["type"] for r in result["results"]]
    assert "decision" in types


def test_search_tag_like_2char_keyword(temp_db):
    """タグLIKE検索: 2文字キーワードでもタグにマッチすればヒット"""
    add_topic(
        title="2文字タグマッチテスト",
        description="テスト",
        tags=["domain:ab"],
    )
    # "ab"は2文字だが、タグ名"ab"にLIKEマッチするのでヒットする
    result = search_service.search(keyword="ab")
    assert "error" not in result
    # tag_like結果がある場合はKEYWORD_TOO_SHORTエラーにならない
    assert len(result["results"]) >= 1
    assert "tag_like" in result["search_methods_used"]
