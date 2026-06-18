"""タグ notes 機能のユニットテスト

- update_tag の正常系・エラー系
- 遭遇時注入の正常系・重複防止
- get_by_ids での遭遇時注入
- 4ツール（get_topics/get_activities/get_logs/get_decisions）の結果ベース注入
"""
import os
import tempfile
import pytest
from src.db import init_database, get_connection
from src.services.tag_service import (
    update_tag,
    collect_tag_notes_for_injection,
    _injected_tags,
)
from src.services.topic_service import add_topic
from src.services.decision_service import add_decisions
from src.services.discussion_log_service import add_logs
from src.services.activity_service import add_activity
from src.services.search_service import get_by_ids
from tests.helpers import add_decision
import src.services.embedding_service as emb


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


@pytest.fixture(autouse=True)
def reset_injected_tags():
    """各テスト前に注入済みタグをリセットする"""
    _injected_tags.clear()
    yield
    _injected_tags.clear()


# ========================================
# update_tag テスト
# ========================================


class TestUpdateTag:
    """update_tagのテスト"""

    def test_update_existing_tag(self, temp_db):
        """既存タグに notes を設定できる"""
        # タグを作成
        add_topic(title="Test", description="Desc", tags=["domain:test"])

        result = update_tag("domain:test", "このドメインでは注意が必要")
        assert "error" not in result
        assert result["tag"] == "domain:test"
        assert result["notes"] == "このドメインでは注意が必要"
        assert result["updated"] is True

    def test_update_bare_tag(self, temp_db):
        """素タグに notes を設定できる"""
        add_topic(title="Test", description="Desc", tags=["hooks"])

        result = update_tag("hooks", "hookの教訓")
        assert "error" not in result
        assert result["tag"] == "hooks"
        assert result["notes"] == "hookの教訓"
        assert result["updated"] is True

    def test_update_nonexistent_tag(self, temp_db):
        """存在しないタグでNOT_FOUNDエラー"""
        result = update_tag("domain:nonexistent", "notes text")
        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"

    def test_update_overwrite(self, temp_db):
        """notes を上書きできる（全文置換）"""
        add_topic(title="Test", description="Desc", tags=["domain:test"])

        update_tag("domain:test", "初回 notes")
        result = update_tag("domain:test", "更新後 notes")
        assert result["notes"] == "更新後 notes"


# ========================================
# 遭遇時注入テスト
# ========================================


class TestTagNotesInjection:
    """遭遇時注入ロジックのテスト"""

    def test_first_encounter_injects_notes(self, temp_db):
        """初回遭遇で notes が付加される"""
        add_topic(title="Test", description="Desc", tags=["domain:test"])
        update_tag("domain:test", "重要な教訓")

        conn = get_connection()
        try:
            result = collect_tag_notes_for_injection(conn, ["domain:test"])
            assert result is not None
            assert len(result) == 1
            assert result[0]["tag"] == "domain:test"
            assert result[0]["notes"] == "重要な教訓"
        finally:
            conn.close()

    def test_second_encounter_no_injection(self, temp_db):
        """2回目の遭遇では注入されない"""
        add_topic(title="Test", description="Desc", tags=["domain:test"])
        update_tag("domain:test", "重要な教訓")

        conn = get_connection()
        try:
            # 1回目
            result1 = collect_tag_notes_for_injection(conn, ["domain:test"])
            assert result1 is not None

            # 2回目
            result2 = collect_tag_notes_for_injection(conn, ["domain:test"])
            assert result2 is None
        finally:
            conn.close()

    def test_no_notes_returns_none(self, temp_db):
        """notes がないタグでは None が返る"""
        add_topic(title="Test", description="Desc", tags=["domain:test"])

        conn = get_connection()
        try:
            result = collect_tag_notes_for_injection(conn, ["domain:test"])
            assert result is None
        finally:
            conn.close()

    def test_mixed_tags_with_and_without_notes(self, temp_db):
        """notes があるタグとないタグの混在"""
        add_topic(title="Test", description="Desc", tags=["domain:test", "domain:empty"])
        update_tag("domain:test", "テスト教訓")
        # domain:empty には notes を設定しない

        conn = get_connection()
        try:
            result = collect_tag_notes_for_injection(conn, ["domain:test", "domain:empty"])
            assert result is not None
            assert len(result) == 1
            assert result[0]["tag"] == "domain:test"
        finally:
            conn.close()

    def test_multiple_tags_with_notes(self, temp_db):
        """複数タグに notes がある場合"""
        add_topic(title="Test", description="Desc", tags=["domain:test", "intent:design"])
        update_tag("domain:test", "テスト教訓")
        update_tag("intent:design", "設計の教訓")

        conn = get_connection()
        try:
            result = collect_tag_notes_for_injection(conn, ["domain:test", "intent:design"])
            assert result is not None
            assert len(result) == 2
            tag_strs = {r["tag"] for r in result}
            assert "domain:test" in tag_strs
            assert "intent:design" in tag_strs
        finally:
            conn.close()

    def test_partial_new_tags(self, temp_db):
        """一部が既に遭遇済み、一部が新規の場合"""
        add_topic(title="Test", description="Desc", tags=["domain:test", "intent:design"])
        update_tag("domain:test", "テスト教訓")
        update_tag("intent:design", "設計の教訓")

        conn = get_connection()
        try:
            # domain:test だけ先に遭遇
            collect_tag_notes_for_injection(conn, ["domain:test"])

            # 両方渡すが、domain:test は既に遭遇済み
            result = collect_tag_notes_for_injection(conn, ["domain:test", "intent:design"])
            assert result is not None
            assert len(result) == 1
            assert result[0]["tag"] == "intent:design"
        finally:
            conn.close()

    def test_nonexistent_tag_no_error(self, temp_db):
        """DBに存在しないタグを渡してもエラーにならない"""
        conn = get_connection()
        try:
            result = collect_tag_notes_for_injection(conn, ["domain:nonexistent"])
            assert result is None
        finally:
            conn.close()


# ========================================
# 常時注入（always_inject_namespaces）テスト
# ========================================


class TestAlwaysInjectNamespaces:
    """always_inject_namespaces パラメータのテスト"""

    def test_always_inject_returns_notes_every_time(self, temp_db):
        """always_inject_namespaces 対象のタグは毎回 notes を返す"""
        add_topic(title="Test", description="Desc", tags=["intent:design"])
        update_tag("intent:design", "設計の教訓")

        conn = get_connection()
        try:
            # 1回目
            result1 = collect_tag_notes_for_injection(
                conn, ["intent:design"], always_inject_namespaces=["intent"]
            )
            assert result1 is not None
            assert len(result1) == 1
            assert result1[0]["tag"] == "intent:design"

            # 2回目: 通常なら None だが、always_inject なので返る
            result2 = collect_tag_notes_for_injection(
                conn, ["intent:design"], always_inject_namespaces=["intent"]
            )
            assert result2 is not None
            assert len(result2) == 1
            assert result2[0]["tag"] == "intent:design"
        finally:
            conn.close()

    def test_always_inject_does_not_register_in_injected_tags(self, temp_db):
        """always_inject 対象のタグは _injected_tags に登録されない"""
        add_topic(title="Test", description="Desc", tags=["intent:design"])
        update_tag("intent:design", "設計の教訓")

        conn = get_connection()
        try:
            collect_tag_notes_for_injection(
                conn, ["intent:design"], always_inject_namespaces=["intent"]
            )
            assert "intent:design" not in _injected_tags.get("__default__", set())
        finally:
            conn.close()

    def test_normal_tags_still_deduplicated(self, temp_db):
        """always_inject_namespaces を使っても通常タグは従来通り重複防止される"""
        add_topic(title="Test", description="Desc", tags=["domain:test", "intent:design"])
        update_tag("domain:test", "テスト教訓")
        update_tag("intent:design", "設計の教訓")

        conn = get_connection()
        try:
            # 1回目: 両方返る
            result1 = collect_tag_notes_for_injection(
                conn, ["domain:test", "intent:design"],
                always_inject_namespaces=["intent"],
            )
            assert result1 is not None
            assert len(result1) == 2

            # 2回目: domain:test は既に注入済みなので intent:design だけ
            result2 = collect_tag_notes_for_injection(
                conn, ["domain:test", "intent:design"],
                always_inject_namespaces=["intent"],
            )
            assert result2 is not None
            assert len(result2) == 1
            assert result2[0]["tag"] == "intent:design"
        finally:
            conn.close()

    def test_always_inject_no_notes_returns_none(self, temp_db):
        """always_inject 対象でも notes がなければ None が返る"""
        # domain:nonotes に notes を設定しない
        add_topic(title="Test", description="Desc", tags=["domain:nonotes"])

        conn = get_connection()
        try:
            result = collect_tag_notes_for_injection(
                conn, ["domain:nonotes"], always_inject_namespaces=["domain"]
            )
            assert result is None
        finally:
            conn.close()

    def test_always_inject_with_no_parameter(self, temp_db):
        """always_inject_namespaces 未指定の場合、従来通りの動作"""
        add_topic(title="Test", description="Desc", tags=["intent:design"])
        update_tag("intent:design", "設計の教訓")

        conn = get_connection()
        try:
            # 1回目
            result1 = collect_tag_notes_for_injection(conn, ["intent:design"])
            assert result1 is not None

            # 2回目: 従来通り None
            result2 = collect_tag_notes_for_injection(conn, ["intent:design"])
            assert result2 is None
        finally:
            conn.close()

    def test_multiple_always_inject_namespaces(self, temp_db):
        """複数の namespace を always_inject に指定できる"""
        add_topic(title="Test", description="Desc", tags=["intent:design", "domain:test"])
        update_tag("intent:design", "設計の教訓")
        update_tag("domain:test", "テスト教訓")

        conn = get_connection()
        try:
            # 1回目
            result1 = collect_tag_notes_for_injection(
                conn, ["intent:design", "domain:test"],
                always_inject_namespaces=["intent", "domain"],
            )
            assert result1 is not None
            assert len(result1) == 2

            # 2回目: 両方 always なので両方返る
            result2 = collect_tag_notes_for_injection(
                conn, ["intent:design", "domain:test"],
                always_inject_namespaces=["intent", "domain"],
            )
            assert result2 is not None
            assert len(result2) == 2
        finally:
            conn.close()


# ========================================
# get_by_ids 遭遇時注入テスト
# ========================================


class TestGetByIdsInjection:
    """get_by_ids での遭遇時注入テスト

    main.py の @mcp.tool() デコレータ付き関数は FunctionTool になるため直接呼べない。
    search_service.get_by_ids + _maybe_inject_tag_notes の組み合わせをテストする。
    """

    def test_get_by_ids_injects_tag_notes(self, temp_db):
        """get_by_ids の結果からタグ notes が注入される"""
        from src.main import _maybe_inject_tag_notes

        topic = add_topic(title="Test Topic", description="Desc", tags=["domain:test"])
        update_tag("domain:test", "テスト教訓")

        # search_service.get_by_ids で結果取得
        result = get_by_ids([{"type": "topic", "id": topic["topic_id"]}])
        assert "error" not in result

        # main.py と同じパターンで注入
        all_tags = []
        for item in result.get("results", []):
            if "data" in item:
                all_tags.extend(item["data"].get("tags", []))
        if all_tags:
            _maybe_inject_tag_notes(result, all_tags)

        assert "tag_notes" in result
        assert len(result["tag_notes"]) == 1
        assert result["tag_notes"][0]["tag"] == "domain:test"
        assert result["tag_notes"][0]["notes"] == "テスト教訓"

    def test_get_by_ids_no_notes_no_key(self, temp_db):
        """notes がない場合は tag_notes キーが含まれない"""
        from src.main import _maybe_inject_tag_notes

        topic = add_topic(title="Test Topic", description="Desc", tags=["domain:test"])

        result = get_by_ids([{"type": "topic", "id": topic["topic_id"]}])
        assert "error" not in result

        all_tags = []
        for item in result.get("results", []):
            if "data" in item:
                all_tags.extend(item["data"].get("tags", []))
        if all_tags:
            _maybe_inject_tag_notes(result, all_tags)

        assert "tag_notes" not in result


# ========================================
# 結果ベース tag_notes 注入テスト（4ツール）
# ========================================


def _apply_result_based_injection(result: dict, items_key: str) -> dict:
    """テスト用ヘルパー: main.pyのハンドラと同じ結果ベース注入ロジックを適用する"""
    from src.main import _collect_result_tags, _maybe_inject_tag_notes

    if "error" not in result:
        all_tags = _collect_result_tags(result.get(items_key, []))
        if all_tags:
            _maybe_inject_tag_notes(result, all_tags, mark=False)
    return result


class TestGetTopicsResultBasedInjection:
    """get_topics の結果ベース tag_notes 注入テスト"""

    def test_injects_tag_notes_from_result_tags(self, temp_db):
        """タグフィルタなしでも結果内のタグからtag_notesが注入される"""
        from src.services.topic_service import get_topics

        add_topic(title="Test Topic", description="Desc", tags=["domain:test"])
        update_tag("domain:test", "テスト教訓")

        result = get_topics()
        assert "error" not in result
        assert len(result["topics"]) >= 1

        _apply_result_based_injection(result, "topics")

        assert "tag_notes" in result
        assert any(n["tag"] == "domain:test" for n in result["tag_notes"])

    def test_no_notes_no_key(self, temp_db):
        """notesがないタグのみの場合はtag_notesキーが含まれない"""
        from src.services.topic_service import get_topics

        add_topic(title="Test Topic", description="Desc", tags=["domain:empty"])

        result = get_topics()
        assert "error" not in result

        _apply_result_based_injection(result, "topics")

        assert "tag_notes" not in result


class TestGetActivitiesResultBasedInjection:
    """get_activities の結果ベース tag_notes 注入テスト"""

    def test_injects_tag_notes_from_result_tags(self, temp_db):
        """タグフィルタなしでも結果内のタグからtag_notesが注入される"""
        from src.services.activity_service import get_activities

        topic = add_topic(title="t", description="d", tags=["domain:test"])
        dec = add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        add_activity(
            title="Test Activity", description="Desc",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [dec["decision_id"]]}],
            check_in=False,
        )
        update_tag("domain:test", "テスト教訓")

        result = get_activities()
        assert "error" not in result
        assert len(result["activities"]) >= 1

        _apply_result_based_injection(result, "activities")

        assert "tag_notes" in result
        assert any(n["tag"] == "domain:test" for n in result["tag_notes"])

    def test_no_notes_no_key(self, temp_db):
        """notesがないタグのみの場合はtag_notesキーが含まれない"""
        from src.services.activity_service import get_activities

        # intent:タグはマイグレーションでnotesが設定されるため、notesのないタグのみ使用
        add_activity(
            title="Test Activity", description="Desc",
            tags=["domain:empty"], check_in=False,
        )

        result = get_activities()
        assert "error" not in result

        _apply_result_based_injection(result, "activities")

        assert "tag_notes" not in result


class TestGetLogsResultBasedInjection:
    """get_logs の結果ベース tag_notes 注入テスト"""

    def test_injects_tag_notes_from_result_tags(self, temp_db):
        """結果内のタグからtag_notesが注入される"""
        from src.services.discussion_log_service import get_logs

        topic = add_topic(title="Test Topic", description="Desc", tags=["domain:test"])
        topic_id = topic["topic_id"]
        add_result = add_logs([
            {"topic_id": topic_id, "title": "Test Log", "content": "content", "tags": ["domain:test"]}
        ])
        assert "error" not in add_result
        assert add_result["errors"] == []
        update_tag("domain:test", "テスト教訓")

        result = get_logs("topic", topic_id)
        assert "error" not in result
        assert len(result["logs"]) >= 1

        _apply_result_based_injection(result, "logs")

        assert "tag_notes" in result
        assert any(n["tag"] == "domain:test" for n in result["tag_notes"])

    def test_no_notes_no_key(self, temp_db):
        """notesがないタグのみの場合はtag_notesキーが含まれない"""
        from src.services.discussion_log_service import get_logs

        topic = add_topic(title="Test Topic", description="Desc", tags=["domain:empty"])
        topic_id = topic["topic_id"]
        add_result = add_logs([
            {"topic_id": topic_id, "title": "Test Log", "content": "content"}
        ])
        assert "error" not in add_result
        assert add_result["errors"] == []

        result = get_logs("topic", topic_id)
        assert "error" not in result

        _apply_result_based_injection(result, "logs")

        assert "tag_notes" not in result


class TestGetDecisionsResultBasedInjection:
    """get_decisions の結果ベース tag_notes 注入テスト"""

    def test_injects_tag_notes_from_result_tags(self, temp_db):
        """結果内のタグからtag_notesが注入される"""
        from src.services.decision_service import get_decisions

        topic = add_topic(title="Test Topic", description="Desc", tags=["domain:test"])
        topic_id = topic["topic_id"]
        add_result = add_decisions([
            {"topic_id": topic_id, "decision": "Test Decision", "reason": "reason", "tags": ["domain:test"]}
        ])
        assert "error" not in add_result
        assert add_result["errors"] == []
        update_tag("domain:test", "テスト教訓")

        result = get_decisions("topic", topic_id)
        assert "error" not in result
        assert len(result["decisions"]) >= 1

        _apply_result_based_injection(result, "decisions")

        assert "tag_notes" in result
        assert any(n["tag"] == "domain:test" for n in result["tag_notes"])

    def test_no_notes_no_key(self, temp_db):
        """notesがないタグのみの場合はtag_notesキーが含まれない"""
        from src.services.decision_service import get_decisions

        topic = add_topic(title="Test Topic", description="Desc", tags=["domain:empty"])
        topic_id = topic["topic_id"]
        add_result = add_decisions([
            {"topic_id": topic_id, "decision": "Test Decision", "reason": "reason"}
        ])
        assert "error" not in add_result
        assert add_result["errors"] == []

        result = get_decisions("topic", topic_id)
        assert "error" not in result

        _apply_result_based_injection(result, "decisions")

        assert "tag_notes" not in result


class TestCollectResultTags:
    """_collect_result_tags ヘルパーのテスト"""

    def test_collects_unique_tags(self):
        """複数アイテムからユニークなタグを収集する"""
        from src.main import _collect_result_tags

        items = [
            {"tags": ["domain:test", "intent:design"]},
            {"tags": ["domain:test", "hooks"]},
            {"tags": ["intent:design"]},
        ]
        result = _collect_result_tags(items)
        assert set(result) == {"domain:test", "intent:design", "hooks"}

    def test_empty_items(self):
        """空リストの場合は空リストを返す"""
        from src.main import _collect_result_tags

        result = _collect_result_tags([])
        assert result == []

    def test_items_without_tags(self):
        """tagsキーがないアイテムでもエラーにならない"""
        from src.main import _collect_result_tags

        items = [{"id": 1}, {"id": 2, "tags": ["domain:test"]}]
        result = _collect_result_tags(items)
        assert result == ["domain:test"]


# ========================================
# mark=False による _injected_tags 非汚染テスト
# ========================================


class TestResultBasedInjectionDoesNotMark:
    """結果ベース注入（mark=False）は _injected_tags を汚染しない"""

    def test_result_based_injection_does_not_mark_injected_tags(self, temp_db):
        """結果ベース注入は_injected_tagsを汚染しない"""
        add_topic(title="Test", description="Desc", tags=["domain:test"])
        update_tag("domain:test", "テスト教訓")

        conn = get_connection()
        try:
            # mark=False で注入（読み取り経路）
            result = collect_tag_notes_for_injection(conn, ["domain:test"], mark=False)
            assert result is not None
            assert len(result) == 1
            assert result[0]["tag"] == "domain:test"

            # _injected_tags に登録されていないことを確認
            assert "domain:test" not in _injected_tags.get("__default__", set())

            # mark=True（書き込み経路）でも notes が注入されることを確認
            result2 = collect_tag_notes_for_injection(conn, ["domain:test"])
            assert result2 is not None
            assert len(result2) == 1
            assert result2[0]["tag"] == "domain:test"
        finally:
            conn.close()

    def test_mark_false_queries_all_tags_including_already_marked(self, temp_db):
        """mark=False は既にマーク済みのタグも含めて全タグをクエリする"""
        add_topic(title="Test", description="Desc", tags=["domain:test", "domain:other"])
        update_tag("domain:test", "テスト教訓")
        update_tag("domain:other", "その他の教訓")

        conn = get_connection()
        try:
            # まず mark=True で domain:test をマーク
            collect_tag_notes_for_injection(conn, ["domain:test"])
            assert "domain:test" in _injected_tags.get("__default__", set())

            # mark=False では domain:test もクエリ対象になる
            result = collect_tag_notes_for_injection(
                conn, ["domain:test", "domain:other"], mark=False
            )
            assert result is not None
            assert len(result) == 2
            tag_strs = {r["tag"] for r in result}
            assert "domain:test" in tag_strs
            assert "domain:other" in tag_strs
        finally:
            conn.close()

    def test_write_after_read_still_injects(self, temp_db):
        """読み取り経路後に書き込み経路でも notes が注入される（シナリオテスト）"""
        from src.main import _maybe_inject_tag_notes

        add_topic(title="Test", description="Desc", tags=["domain:test"])
        update_tag("domain:test", "テスト教訓")

        # Step 1: 読み取り経路（mark=False）
        read_result = {"topics": [{"tags": ["domain:test"]}]}
        _maybe_inject_tag_notes(read_result, ["domain:test"], mark=False)
        assert "tag_notes" in read_result

        # Step 2: 書き込み経路（mark=True、デフォルト）
        write_result = {"topic_id": 1}
        _maybe_inject_tag_notes(write_result, ["domain:test"])
        assert "tag_notes" in write_result
        assert write_result["tag_notes"][0]["tag"] == "domain:test"


# ========================================
# MCP ハンドラ経由テスト
# ========================================


class TestHandlerGetTopicsInjection:
    """get_topics ハンドラ経由で tag_notes が注入されるテスト"""

    def test_handler_injects_tag_notes(self, temp_db):
        """MCP ハンドラ経由で tag_notes が注入される"""
        from src.main import get_topics

        add_topic(title="Handler Test", description="Desc", tags=["domain:handler"])
        update_tag("domain:handler", "ハンドラ経由テスト")

        result = get_topics()
        assert "error" not in result
        assert "tag_notes" in result
        assert any(n["tag"] == "domain:handler" for n in result["tag_notes"])

    def test_handler_does_not_pollute_injected_tags(self, temp_db):
        """get_topics ハンドラは _injected_tags を汚染しない"""
        from src.main import get_topics

        add_topic(title="Handler Test", description="Desc", tags=["domain:handler"])
        update_tag("domain:handler", "ハンドラ経由テスト")

        get_topics()
        assert "domain:handler" not in _injected_tags.get("__default__", set())


class TestHandlerGetActivitiesInjection:
    """get_activities ハンドラ経由で tag_notes が注入されるテスト"""

    def test_handler_injects_tag_notes(self, temp_db):
        """MCP ハンドラ経由で tag_notes が注入される"""
        from src.main import get_activities

        add_activity(
            title="Handler Activity", description="Desc",
            tags=["domain:handler"], check_in=False,
        )
        update_tag("domain:handler", "ハンドラ経由テスト")

        result = get_activities()
        assert "error" not in result
        assert "tag_notes" in result
        assert any(n["tag"] == "domain:handler" for n in result["tag_notes"])

    def test_handler_does_not_pollute_injected_tags(self, temp_db):
        """get_activities ハンドラは _injected_tags を汚染しない"""
        from src.main import get_activities

        add_activity(
            title="Handler Activity", description="Desc",
            tags=["domain:handler"], check_in=False,
        )
        update_tag("domain:handler", "ハンドラ経由テスト")

        get_activities()
        assert "domain:handler" not in _injected_tags.get("__default__", set())


class TestHandlerGetLogsInjection:
    """get_logs ハンドラ経由で tag_notes が注入されるテスト"""

    def test_handler_injects_tag_notes(self, temp_db):
        """MCP ハンドラ経由で tag_notes が注入される"""
        from src.main import get_logs

        topic = add_topic(title="Handler Topic", description="Desc", tags=["domain:handler"])
        topic_id = topic["topic_id"]
        add_result = add_logs([
            {"topic_id": topic_id, "title": "Handler Log", "content": "content", "tags": ["domain:handler"]}
        ])
        assert "error" not in add_result
        assert add_result["errors"] == []
        update_tag("domain:handler", "ハンドラ経由テスト")

        result = get_logs("topic", topic_id)
        assert "error" not in result
        assert "tag_notes" in result
        assert any(n["tag"] == "domain:handler" for n in result["tag_notes"])

    def test_handler_does_not_pollute_injected_tags(self, temp_db):
        """get_logs ハンドラは _injected_tags を汚染しない"""
        from src.main import get_logs

        topic = add_topic(title="Handler Topic", description="Desc", tags=["domain:handler"])
        topic_id = topic["topic_id"]
        add_result = add_logs([
            {"topic_id": topic_id, "title": "Handler Log", "content": "content", "tags": ["domain:handler"]}
        ])
        assert "error" not in add_result
        assert add_result["errors"] == []
        update_tag("domain:handler", "ハンドラ経由テスト")

        get_logs("topic", topic_id)
        assert "domain:handler" not in _injected_tags.get("__default__", set())


class TestHandlerGetDecisionsInjection:
    """get_decisions ハンドラ経由で tag_notes が注入されるテスト"""

    def test_handler_injects_tag_notes(self, temp_db):
        """MCP ハンドラ経由で tag_notes が注入される"""
        from src.main import get_decisions

        topic = add_topic(title="Handler Topic", description="Desc", tags=["domain:handler"])
        topic_id = topic["topic_id"]
        add_result = add_decisions([
            {"topic_id": topic_id, "decision": "Handler Decision", "reason": "reason", "tags": ["domain:handler"]}
        ])
        assert "error" not in add_result
        assert add_result["errors"] == []
        update_tag("domain:handler", "ハンドラ経由テスト")

        result = get_decisions("topic", topic_id)
        assert "error" not in result
        assert "tag_notes" in result
        assert any(n["tag"] == "domain:handler" for n in result["tag_notes"])

    def test_handler_does_not_pollute_injected_tags(self, temp_db):
        """get_decisions ハンドラは _injected_tags を汚染しない"""
        from src.main import get_decisions

        topic = add_topic(title="Handler Topic", description="Desc", tags=["domain:handler"])
        topic_id = topic["topic_id"]
        add_result = add_decisions([
            {"topic_id": topic_id, "decision": "Handler Decision", "reason": "reason", "tags": ["domain:handler"]}
        ])
        assert "error" not in add_result
        assert add_result["errors"] == []
        update_tag("domain:handler", "ハンドラ経由テスト")

        get_decisions("topic", topic_id)
        assert "domain:handler" not in _injected_tags.get("__default__", set())


# ========================================
# マルチセッション分離テスト
# ========================================


class TestMultiSessionIsolation:
    """異なるセッションIDで _injected_tags が独立管理されるテスト"""

    def test_different_sessions_inject_independently(self, temp_db):
        """セッションAの注入済みタグがセッションBの注入を阻害しない"""
        add_topic(title="Test", description="Desc", tags=["domain:test"])
        update_tag("domain:test", "テスト教訓")

        conn = get_connection()
        try:
            result_a = collect_tag_notes_for_injection(
                conn, ["domain:test"], session_id="session-A"
            )
            assert result_a is not None
            assert result_a[0]["tag"] == "domain:test"

            result_b = collect_tag_notes_for_injection(
                conn, ["domain:test"], session_id="session-B"
            )
            assert result_b is not None
            assert result_b[0]["tag"] == "domain:test"
        finally:
            conn.close()

    def test_same_session_deduplicates(self, temp_db):
        """同一セッション内では2回目の注入が抑制される"""
        add_topic(title="Test", description="Desc", tags=["domain:test"])
        update_tag("domain:test", "テスト教訓")

        conn = get_connection()
        try:
            result1 = collect_tag_notes_for_injection(
                conn, ["domain:test"], session_id="session-X"
            )
            assert result1 is not None

            result2 = collect_tag_notes_for_injection(
                conn, ["domain:test"], session_id="session-X"
            )
            assert result2 is None
        finally:
            conn.close()

    def test_session_sets_are_isolated(self, temp_db):
        """各セッションの注入済みセットが他セッションに影響しない"""
        add_topic(title="Test", description="Desc", tags=["domain:test", "domain:other"])
        update_tag("domain:test", "テスト教訓")
        update_tag("domain:other", "その他の教訓")

        conn = get_connection()
        try:
            collect_tag_notes_for_injection(
                conn, ["domain:test"], session_id="session-1"
            )
            assert "domain:test" in _injected_tags["session-1"]
            assert "session-2" not in _injected_tags

            collect_tag_notes_for_injection(
                conn, ["domain:other"], session_id="session-2"
            )
            assert "domain:other" in _injected_tags["session-2"]
            assert "domain:other" not in _injected_tags["session-1"]
        finally:
            conn.close()

    def test_no_session_id_uses_default_key(self, temp_db):
        """session_id=Noneの場合は__default__キーが使われる"""
        add_topic(title="Test", description="Desc", tags=["domain:test"])
        update_tag("domain:test", "テスト教訓")

        conn = get_connection()
        try:
            collect_tag_notes_for_injection(conn, ["domain:test"])
            assert "domain:test" in _injected_tags["__default__"]
        finally:
            conn.close()
