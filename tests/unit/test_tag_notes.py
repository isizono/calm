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
# update_tag archived テスト
# ========================================


class TestUpdateTagArchived:
    """update_tag の archived / archived_reason 更新のテスト"""

    def test_archive_tag(self, temp_db):
        """archived=True でタグを退役できる"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy"])

        result = update_tag("domain:legacy", archived=True, archived_reason="解体済み")
        assert "error" not in result
        assert result["tag"] == "domain:legacy"
        assert result["archived"] is True
        assert result["archived_at"] is not None
        assert result["archived_reason"] == "解体済み"
        assert result["updated"] is True

    def test_archive_idempotent_no_change(self, temp_db):
        """既にarchivedのタグへarchived=Trueを再適用してもarchived_atは不変・updated: False（エッジケース#2）"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy"])
        first = update_tag("domain:legacy", archived=True, archived_reason="最初の理由")

        second = update_tag("domain:legacy", archived=True, archived_reason="別の理由")
        assert "error" not in second
        assert second["archived"] is True
        assert second["archived_at"] == first["archived_at"]
        assert second["updated"] is False
        # archived_reasonの後追い書き換えはしない（5. Edge cases参照）
        assert second["archived_reason"] == "最初の理由"

    def test_archive_and_notes_conflicting_params(self, temp_db):
        """notesとarchivedの同時指定はCONFLICTING_PARAMSエラー（エッジケース#3）"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy"])

        result = update_tag("domain:legacy", notes="x", archived=True)
        assert "error" in result
        assert result["error"]["code"] == "CONFLICTING_PARAMS"

    def test_archived_reason_alone_is_orphan(self, temp_db):
        """archived_reason単独指定はORPHAN_ARCHIVED_REASONエラー（エッジケース#4）"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy"])

        result = update_tag("domain:legacy", archived_reason="理由だけ")
        assert "error" in result
        assert result["error"]["code"] == "ORPHAN_ARCHIVED_REASON"

    def test_archived_reason_with_archived_false_is_orphan(self, temp_db):
        """archived=Falseとarchived_reasonの同時指定もORPHAN_ARCHIVED_REASONエラー"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy"])

        result = update_tag("domain:legacy", archived=False, archived_reason="理由")
        assert "error" in result
        assert result["error"]["code"] == "ORPHAN_ARCHIVED_REASON"

    def test_unarchive_clears_reason(self, temp_db):
        """archived=Falseでarchived_at・archived_reasonが両方NULLに戻る（エッジケース#5）"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy"])
        update_tag("domain:legacy", archived=True, archived_reason="理由")

        result = update_tag("domain:legacy", archived=False)
        assert "error" not in result
        assert result["archived"] is False
        assert result["updated"] is True

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT archived_at, archived_reason FROM tags WHERE namespace='domain' AND name='legacy'"
            ).fetchone()
            assert row["archived_at"] is None
            assert row["archived_reason"] is None
        finally:
            conn.close()

    def test_unarchive_idempotent_no_change(self, temp_db):
        """既に非archivedのタグへarchived=Falseを適用してもupdated: False（冪等）"""
        add_topic(title="Test", description="Desc", tags=["domain:active"])

        result = update_tag("domain:active", archived=False)
        assert "error" not in result
        assert result["archived"] is False
        assert result["updated"] is False

    def test_archived_tag_cannot_be_canonical_target(self, temp_db):
        """archivedタグをcanonical先に指定するとARCHIVED_CANONICAL_INVALID（エッジケース#6）"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy", "domain:alias-src"])
        update_tag("domain:legacy", archived=True, archived_reason="退役済み")

        result = update_tag("domain:alias-src", canonical="domain:legacy")
        assert "error" in result
        assert result["error"]["code"] == "ARCHIVED_CANONICAL_INVALID"

    def test_archived_tag_cannot_become_alias(self, temp_db):
        """archivedタグ自身をcanonicalに設定（新規エイリアス化）するとARCHIVED_CANONICAL_INVALID"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy", "domain:target"])
        update_tag("domain:legacy", archived=True, archived_reason="退役済み")

        result = update_tag("domain:legacy", canonical="domain:target")
        assert "error" in result
        assert result["error"]["code"] == "ARCHIVED_CANONICAL_INVALID"

    def test_archiving_canonical_target_with_dependents_is_blocked(self, temp_db):
        """他タグのcanonical先になっているタグはarchived化できない"""
        add_topic(title="Test", description="Desc", tags=["domain:target", "domain:alias-src"])
        update_tag("domain:alias-src", canonical="domain:target")

        result = update_tag("domain:target", archived=True, archived_reason="退役したい")
        assert "error" in result
        assert result["error"]["code"] == "ARCHIVED_CANONICAL_INVALID"

    def test_archiving_alias_source_is_allowed(self, temp_db):
        """エイリアス元（canonical_idを持つタグ自身）はarchived化できる（5. Edge cases参照）"""
        add_topic(title="Test", description="Desc", tags=["domain:target", "domain:alias-src"])
        update_tag("domain:alias-src", canonical="domain:target")

        result = update_tag("domain:alias-src", archived=True, archived_reason="不要になったエイリアス")
        assert "error" not in result
        assert result["archived"] is True

    def test_canonical_target_archived_via_bypass_keeps_existing_links_rejects_new(self, temp_db):
        """canonical先が(直接SQLで)archivedになった状態でも既存の紐付けは維持され、
        新規の紐付けだけ拒否される（5. Edge cases参照）。

        update_tag経由ではdependentを持つタグをarchived化できない（他テストで確認済み）
        ため、この状態は「migrationの手動適用や直接SQL操作」（設計§3.3が明示するAPI契約
        外のケース）でのみ再現できる。ここではその状態を直接SQLで作り、
        (1) archived化以前からの紐付けは読み出し側で壊れないこと
        (2) archived化後の新規紐付け操作はAPI経由で拒否されること
        の両方を固定する。
        """
        add_topic(
            title="Test", description="Desc",
            tags=["domain:target", "domain:alias-src", "domain:new-alias-attempt"],
        )
        update_tag("domain:alias-src", canonical="domain:target")

        # alias-src経由で新規エンティティをタグ付け（canonical解決によりtarget側のtag_idに
        # 紐付く）。この紐付けが「既存の紐付け」にあたる
        tagged_topic = add_topic(title="TaggedViaAlias", description="Desc", tags=["domain:alias-src"])
        assert "error" not in tagged_topic

        conn = get_connection()
        try:
            target_row = conn.execute(
                "SELECT id FROM tags WHERE namespace='domain' AND name='target'"
            ).fetchone()
            target_id = target_row["id"]

            # update_tag経由では到達できない状態（dependent保持のままarchived化）を
            # 直接SQLで再現する
            conn.execute(
                "UPDATE tags SET archived_at = CURRENT_TIMESTAMP, archived_reason = ? WHERE id = ?",
                ("直接SQLでの退役", target_id),
            )
            conn.commit()

            # (1) 既存の紐付け: alias-src経由でタグ付けしたエンティティは、archived化後も
            # target(domain:target)への紐付けが読み出せる
            linked = conn.execute(
                "SELECT 1 FROM topic_tags WHERE topic_id = ? AND tag_id = ?",
                (tagged_topic["topic_id"], target_id),
            ).fetchone()
            assert linked is not None
        finally:
            conn.close()

        # (2) 新規の紐付け: archived化後に別タグをtargetへエイリアスしようとするとAPI経由では拒否
        result = update_tag("domain:new-alias-attempt", canonical="domain:target")
        assert "error" in result
        assert result["error"]["code"] == "ARCHIVED_CANONICAL_INVALID"

    def test_archived_state_preserved_after_rename(self, temp_db):
        """archivedタグをrenameしてもarchived状態はtag_idベースで維持される（5. Edge cases参照）"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy"])
        update_tag("domain:legacy", archived=True, archived_reason="退役済み")

        rename_result = update_tag("domain:legacy", rename="domain:legacy-renamed")
        assert "error" not in rename_result
        assert rename_result["renamed_to"] == "domain:legacy-renamed"

        # 新名で再度archived=Trueを呼ぶと冪等（archived状態が保持されている証拠）
        result = update_tag("domain:legacy-renamed", archived=True)
        assert "error" not in result
        assert result["archived"] is True
        assert result["updated"] is False
        assert result["archived_reason"] == "退役済み"

    def test_archived_at_removed_with_physical_tag_deletion(self, temp_db):
        """タグ行が物理削除されればarchived_at列も一緒に消える（同一行の列のため。5. Edge cases参照）"""
        from src.services.tag_service import get_archived_tags_for_strings

        add_topic(title="Test", description="Desc", tags=["domain:legacy"])
        update_tag("domain:legacy", archived=True, archived_reason="退役済み")

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM tags WHERE namespace='domain' AND name='legacy'"
            ).fetchone()
            tag_id = row["id"]
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
            conn.commit()

            remaining = conn.execute(
                "SELECT * FROM tags WHERE id = ?", (tag_id,)
            ).fetchone()
            assert remaining is None

            # 存在しなくなったタグ文字列を渡してもエラーにならず空扱いになる
            archived = get_archived_tags_for_strings(conn, ["domain:legacy"])
            assert archived == []
        finally:
            conn.close()

    def test_notes_update_works_while_archived(self, temp_db):
        """archived状態のタグでもarchivedを指定しないnotes更新は1コールで完結する"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy"])
        update_tag("domain:legacy", archived=True, archived_reason="退役済み")

        result = update_tag("domain:legacy", notes="archived中でも更新できる教訓")
        assert "error" not in result
        assert result["notes"] == "archived中でも更新できる教訓"

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT archived_at FROM tags WHERE namespace='domain' AND name='legacy'"
            ).fetchone()
            assert row["archived_at"] is not None
        finally:
            conn.close()


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

    def test_session_eviction_caps_tracked_sessions(self, temp_db):
        """セッション追跡数が上限に達したら最古セッションから追い出される"""
        from src.services import tag_service

        add_topic(title="Test", description="Desc", tags=["domain:test"])
        update_tag("domain:test", "重要な教訓")

        conn = get_connection()
        try:
            for i in range(tag_service._INJECTED_TAGS_MAX_SESSIONS + 10):
                collect_tag_notes_for_injection(
                    conn, ["domain:test"], session_id=f"session-{i}"
                )
            assert len(_injected_tags) == tag_service._INJECTED_TAGS_MAX_SESSIONS
            # 最古セッションは追い出され、再遭遇時には再度注入される
            assert "session-0" not in _injected_tags
            result = collect_tag_notes_for_injection(
                conn, ["domain:test"], session_id="session-0"
            )
            assert result is not None
        finally:
            conn.close()

    def test_session_eviction_concurrent_new_sessions(self, temp_db):
        """上限到達状態で複数スレッドが同時に新規セッションを登録しても例外が出ない"""
        import threading
        from src.services import tag_service

        add_topic(title="Test", description="Desc", tags=["domain:test"])
        update_tag("domain:test", "重要な教訓")

        conn = get_connection()
        try:
            # 上限まで埋める
            for i in range(tag_service._INJECTED_TAGS_MAX_SESSIONS):
                collect_tag_notes_for_injection(
                    conn, ["domain:test"], session_id=f"warmup-{i}"
                )
        finally:
            conn.close()

        errors = []
        barrier = threading.Barrier(8)

        def worker(i):
            # スレッドごとに独立したDB接続を使う
            worker_conn = get_connection()
            try:
                barrier.wait(timeout=5)
                for j in range(20):
                    collect_tag_notes_for_injection(
                        worker_conn,
                        ["domain:test"],
                        session_id=f"concurrent-{i}-{j}",
                    )
            except Exception as e:  # KeyError等の競合起因の例外を捕捉
                errors.append(e)
            finally:
                worker_conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert len(_injected_tags) <= tag_service._INJECTED_TAGS_MAX_SESSIONS

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


# ========================================
# archived タグの push 除外テスト
# ========================================


class TestArchivedPushExclusion:
    """archived タグの tag notes 自動注入からの除外（エッジケース#1・#10・#11）"""

    def test_archived_tag_notes_excluded_from_injection(self, temp_db):
        """archived=Trueのタグはnotesがあっても注入結果に含まれない（エッジケース#1）"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy", "domain:active"])
        update_tag("domain:legacy", "退役システムの教訓")
        update_tag("domain:active", "現役の教訓")
        update_tag("domain:legacy", archived=True, archived_reason="解体済み")

        conn = get_connection()
        try:
            result = collect_tag_notes_for_injection(conn, ["domain:legacy", "domain:active"])
            assert result is not None
            tag_strs = {r["tag"] for r in result}
            assert "domain:legacy" not in tag_strs
            assert "domain:active" in tag_strs
        finally:
            conn.close()

    def test_archived_only_tag_returns_none(self, temp_db):
        """archivedタグしか渡さなければ None が返る（notes自体は存在するのに）"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy"])
        update_tag("domain:legacy", "退役システムの教訓")
        update_tag("domain:legacy", archived=True, archived_reason="解体済み")

        conn = get_connection()
        try:
            result = collect_tag_notes_for_injection(conn, ["domain:legacy"])
            assert result is None
        finally:
            conn.close()

    def test_new_entity_with_archived_tag_still_excluded(self, temp_db):
        """archivedタグを新規エンティティに付与しても、そのタグのnotesはpush除外され続ける（エッジケース#11）"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy"])
        update_tag("domain:legacy", "退役システムの教訓")
        update_tag("domain:legacy", archived=True, archived_reason="解体済み")

        # archived後に新規エンティティへ同タグを付与（エラー・警告なし）
        second = add_topic(title="Test2", description="別トピック", tags=["domain:legacy"])
        assert "error" not in second

        conn = get_connection()
        try:
            result = collect_tag_notes_for_injection(
                conn, ["domain:legacy"], session_id="fresh-session"
            )
            assert result is None
        finally:
            conn.close()

    def test_unregistered_archived_tag_unarchive_injects_in_same_session(self, temp_db):
        """未参照のarchivedタグを同セッション内で解除 → 解除後の初回参照でnotesが注入される（エッジケース#10）"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy"])
        update_tag("domain:legacy", "退役システムの教訓")
        update_tag("domain:legacy", archived=True, archived_reason="解体済み")

        conn = get_connection()
        try:
            # このセッションではまだ一度も domain:legacy を参照していない
            update_tag("domain:legacy", archived=False)
            result = collect_tag_notes_for_injection(
                conn, ["domain:legacy"], session_id="session-fresh"
            )
            assert result is not None
            assert result[0]["tag"] == "domain:legacy"
        finally:
            conn.close()

    def test_checkin_excludes_archived_tag_notes(self, temp_db):
        """check_in経由でもarchivedタグのnotesはtag_notesから除外される

        checkin_service.pyは本設計では変更しない（collect_tag_notes_for_injection経由の
        除外が自動的に効く前提）。実際にcheck_in()を呼んで観察する。
        """
        from src.services.checkin_service import check_in
        from src.services.activity_service import add_activity

        act = add_activity(
            title="CheckinArchivedExclusion", description="Desc",
            tags=["domain:legacy-checkin", "domain:active-checkin"],
            check_in=False,
        )
        update_tag("domain:legacy-checkin", "退役システムの教訓")
        update_tag("domain:active-checkin", "現役の教訓")
        update_tag("domain:legacy-checkin", archived=True, archived_reason="解体済み")

        result = check_in(act["activity_id"])
        assert "error" not in result
        tag_notes = result.get("tag_notes", [])
        tag_strs = {n["tag"] for n in tag_notes}
        assert "domain:legacy-checkin" not in tag_strs
        assert "domain:active-checkin" in tag_strs

    def test_checkin_response_keys_unchanged_by_archived(self, temp_db):
        """archivedタグの有無でcheck_in応答のトップレベルキー集合が変わらない（新規フィールド追加なし）"""
        from src.services.checkin_service import check_in
        from src.services.activity_service import add_activity

        act_plain = add_activity(
            title="CheckinKeysPlain", description="Desc",
            tags=["domain:checkin-keys-plain"], check_in=False,
        )
        act_archived = add_activity(
            title="CheckinKeysArchived", description="Desc",
            tags=["domain:checkin-keys-legacy"], check_in=False,
        )
        update_tag("domain:checkin-keys-legacy", archived=True, archived_reason="解体済み")

        result_plain = check_in(act_plain["activity_id"])
        result_archived = check_in(act_archived["activity_id"])
        assert "error" not in result_plain
        assert "error" not in result_archived
        assert set(result_plain.keys()) == set(result_archived.keys())

    def test_registered_archived_tag_unarchive_not_injected_same_session(self, temp_db):
        """同セッション内で一度参照済みのarchivedタグを解除しても、そのセッションでは注入されない（エッジケース#10）"""
        add_topic(title="Test", description="Desc", tags=["domain:legacy"])
        update_tag("domain:legacy", "退役システムの教訓")
        update_tag("domain:legacy", archived=True, archived_reason="解体済み")

        conn = get_connection()
        try:
            # archived状態で一度参照（_injected_tagsに登録される。notesは除外され返らない）
            first = collect_tag_notes_for_injection(
                conn, ["domain:legacy"], session_id="session-same"
            )
            assert first is None

            # 同セッション内でarchived解除
            update_tag("domain:legacy", archived=False)

            # 同じセッションで再参照 → 登録済みのためSELECT自体がスキップされ、注入されない
            second = collect_tag_notes_for_injection(
                conn, ["domain:legacy"], session_id="session-same"
            )
            assert second is None
        finally:
            conn.close()
