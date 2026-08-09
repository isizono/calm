"""ask_service の単体テスト。

dedup（fingerprint一致・別ライフ判定）、状態遷移（open→answered→promoted/dismissed、
open→withdrawn）、TOCTOU回避（1段クエリUPDATEのrowcountチェック）、長さ上限、
duplicate blocksの静かなdedupeを検証する。
"""
import sqlite3
from unittest.mock import MagicMock

import pytest

from src.db import get_connection
from src.services import ask_service as ak
from src.services.activity_service import add_activity, update_activity
from src.services.relay import runtime as relay_runtime_module


def _make_activity(title: str = "a1", status: str | None = None) -> int:
    activity_id = add_activity(
        title=title, description="d", tags=["domain:test"], check_in=False
    )["activity_id"]
    if status is not None:
        update_activity(activity_id, status=status)
    return activity_id


class TestAddAskValidation:
    def test_empty_question_rejected(self, temp_db):
        act = _make_activity()
        result = ak.add_ask("   ", tags=["domain:test"], blocks=[act])
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_question_over_max_len_rejected(self, temp_db):
        act = _make_activity()
        result = ak.add_ask("x" * (ak.QUESTION_MAX_LEN + 1), tags=["domain:test"], blocks=[act])
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_context_over_max_len_rejected(self, temp_db):
        act = _make_activity()
        result = ak.add_ask("q", tags=["domain:test"], blocks=[act], context="x" * (ak.CONTEXT_MAX_LEN + 1))
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_empty_blocks_rejected(self, temp_db):
        result = ak.add_ask("q", tags=["domain:test"], blocks=[])
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_nonexistent_activity_in_blocks_rejected(self, temp_db):
        result = ak.add_ask("q", tags=["domain:test"], blocks=[999999])
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_all_blocks_completed_rejected(self, temp_db):
        act = _make_activity(status="completed")
        result = ak.add_ask("q", tags=["domain:test"], blocks=[act])
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_one_non_completed_block_among_completed_is_accepted(self, temp_db):
        completed = _make_activity("done", status="completed")
        open_act = _make_activity("open")
        result = ak.add_ask("q", tags=["domain:test"], blocks=[completed, open_act])
        assert "error" not in result

    def test_duplicate_activity_id_in_blocks_is_silently_deduped(self, temp_db):
        act = _make_activity()
        result = ak.add_ask("q", tags=["domain:test"], blocks=[act, act, act])
        assert "error" not in result

        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ask_blocks WHERE ask_id = ?", (result["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_empty_tags_rejected(self, temp_db):
        act = _make_activity()
        result = ak.add_ask("q", tags=[], blocks=[act])
        assert result["error"]["code"] == "TAGS_REQUIRED"

    def test_tags_without_domain_rejected(self, temp_db):
        act = _make_activity()
        result = ak.add_ask("q", tags=["plain-tag"], blocks=[act])
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_tag_namespace_rejected(self, temp_db):
        act = _make_activity()
        result = ak.add_ask("q", tags=["bogus:tag"], blocks=[act])
        assert result["error"]["code"] == "INVALID_TAG_NAMESPACE"

    def test_invalid_kind_rejected(self, temp_db):
        act = _make_activity()
        result = ak.add_ask("q", tags=["domain:test"], blocks=[act], kind="bogus")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_default_kind_is_ask(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q", tags=["domain:test"], blocks=[act])
        listed = ak.get_asks()
        assert listed["asks"][0]["kind"] == "ask"

    def test_kind_meta_accepted(self, temp_db):
        act = _make_activity()
        ak.add_ask("q", tags=["domain:test", "meta-ask"], blocks=[act], kind="meta")
        listed = ak.get_asks(kind="meta")
        assert listed["asks"][0]["kind"] == "meta"

    def test_tags_persisted_and_returned_by_get_asks(self, temp_db):
        act = _make_activity()
        ak.add_ask("q", tags=["domain:test", "plain-tag"], blocks=[act])
        listed = ak.get_asks()
        assert set(listed["asks"][0]["tags"]) == {"domain:test", "plain-tag"}


class TestAddAskDedup:
    def test_same_question_reuses_open_row(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("Should we do X?", tags=["domain:test"], blocks=[act])
        r2 = ak.add_ask("  should we do x?  ", tags=["domain:test"], blocks=[act])

        assert r2["id"] == r1["id"]
        assert r2["deduped"] is True
        assert r2["occurrence_count"] == 2

        conn = get_connection()
        try:
            total = conn.execute("SELECT COUNT(*) FROM asks").fetchone()[0]
        finally:
            conn.close()
        assert total == 1

    def test_dedup_unions_blocks_and_requesters(self, temp_db):
        act1 = _make_activity("a1")
        act2 = _make_activity("a2")
        r1 = ak.add_ask("same question", tags=["domain:test"], blocks=[act1], session_id="sess-1")
        ak.add_ask("same question", tags=["domain:test"], blocks=[act2], session_id="sess-2")

        listed = ak.get_asks()
        ask = listed["asks"][0]
        assert {b["id_raw"] for b in ask["blocks"]} == {act1, act2}
        assert set(ask["requesters"]) == {"sess-1", "sess-2"}

    def test_dedup_overwrites_context_last_write_wins(self, temp_db):
        act = _make_activity()
        ak.add_ask("same question", tags=["domain:test"], blocks=[act], context="first")
        ak.add_ask("same question", tags=["domain:test"], blocks=[act], context="second")

        listed = ak.get_asks()
        assert listed["asks"][0]["context"] == "second"

    def test_answered_ask_does_not_dedup_new_open_post(self, temp_db):
        """answered/promoted/dismissed/withdrawnの同一questionは別のライフとみなし新規行になる。"""
        act = _make_activity()
        r1 = ak.add_ask("same question", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "yes")

        r2 = ak.add_ask("same question", tags=["domain:test"], blocks=[act])

        assert r2["id"] != r1["id"]
        assert r2["deduped"] is False
        conn = get_connection()
        try:
            total = conn.execute("SELECT COUNT(*) FROM asks").fetchone()[0]
        finally:
            conn.close()
        assert total == 2

    def test_recent_withdraw_blocks_repost(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("same question", tags=["domain:test"], blocks=[act])
        ak.withdraw_ask(r1["id"], "posted by mistake")

        result = ak.add_ask("same question", tags=["domain:test"], blocks=[act])
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_repost_allowed_after_withdraw_cooldown_elapses(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("same question", tags=["domain:test"], blocks=[act])
        ak.withdraw_ask(r1["id"], "posted by mistake")

        conn = get_connection()
        try:
            conn.execute(
                "UPDATE asks SET withdrawn_at = datetime('now', '-10 minutes') WHERE id = ?",
                (r1["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        result = ak.add_ask("same question", tags=["domain:test"], blocks=[act])
        assert "error" not in result
        assert result["id"] != r1["id"]

    def test_dedup_keeps_first_kind_and_ignores_repost_kind(self, temp_db):
        """dedup時（同一fingerprintのopen ask再post）はkindを無視し、初回投入時の
        値を保持する（判断が迷いうる点として採用した方針）。"""
        act = _make_activity()
        ak.add_ask("same question", tags=["domain:test"], blocks=[act], kind="ask")
        ak.add_ask("same question", tags=["domain:test"], blocks=[act], kind="meta")

        listed = ak.get_asks()
        assert listed["asks"][0]["kind"] == "ask"

    def test_dedup_keeps_first_tags_and_ignores_repost_tags(self, temp_db):
        """dedup時は今回渡したtagsを無視し、初回投入時のtagsを保持する。"""
        act = _make_activity()
        ak.add_ask("same question", tags=["domain:test"], blocks=[act])
        ak.add_ask("same question", tags=["domain:other"], blocks=[act])

        listed = ak.get_asks()
        assert listed["asks"][0]["tags"] == ["domain:test"]

    def test_repost_retries_tag_resolution_when_ask_has_no_tags_yet(self, temp_db):
        """タグ解決失敗によりask行だけ確定してタグが空のまま残った状態を、
        add_ask_with_connで直接（呼び出し元add_askのタグ解決処理を経由せず）
        再現し、その状態のaskへ同一questionを再postするとタグ解決が
        再試行され成功することを確認する（occurrence_countではなくask_tagsの
        実在で判定するようにした自己修復の契約）。"""
        act = _make_activity()
        conn = get_connection()
        try:
            created = ak.add_ask_with_conn(
                conn, "same question", blocks=[act], tags=["domain:test"], kind="ask"
            )
            conn.commit()
        finally:
            conn.close()
        ask_id = created["id"]

        conn = get_connection()
        try:
            tag_count = conn.execute(
                "SELECT COUNT(*) FROM ask_tags WHERE ask_id = ?", (ask_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert tag_count == 0

        result = ak.add_ask("same question", tags=["domain:test", "retry-tag"], blocks=[act])
        assert result["id"] == ask_id
        assert result["deduped"] is True

        listed = ak.get_asks()
        assert set(listed["asks"][0]["tags"]) == {"domain:test", "retry-tag"}


class TestAddAskRelaySubscribe:
    """add_ask成功後のrelay_subscribe連携（自個体label ask:{id}の購読宣言）。"""

    def test_relay_subscribe_called_with_own_ask_label(self, temp_db, disable_embedding, monkeypatch):
        act = _make_activity()
        calls = []

        def _fake_relay_subscribe(labels, *, caller_session_id):
            calls.append((labels, caller_session_id))
            return {"subscription_id": "sub-1", "reused": False}

        monkeypatch.setattr(ak, "relay_subscribe", _fake_relay_subscribe)

        result = ak.add_ask("q1", tags=["domain:test"], blocks=[act], session_id="sess-1")

        assert "error" not in result
        assert len(calls) == 1
        labels, caller_session_id = calls[0]
        assert labels == [f"ask:{result['id']}"]
        assert caller_session_id == "sess-1"

    def test_relay_subscribe_not_called_without_session_id(self, temp_db, disable_embedding, monkeypatch):
        act = _make_activity()
        calls = []
        monkeypatch.setattr(ak, "relay_subscribe", lambda *a, **kw: calls.append((a, kw)))

        result = ak.add_ask("q1", tags=["domain:test"], blocks=[act])

        assert "error" not in result
        assert calls == []

    def test_add_ask_succeeds_when_relay_not_connected(
        self, temp_db, disable_embedding, monkeypatch, tmp_path
    ):
        """RELAY_BEARER_TOKEN未設定（relay未接続）でも、relay_subscribeはconfig_missing
        エラーを返すだけで例外を投げず、add_ask自体は成功する。credential.jsonへの
        フォールバックを避けるためRELAY_STATE_DIRも隔離する。"""
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
        monkeypatch.delenv("RELAY_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("RELAY_BASE_URL", raising=False)
        act = _make_activity()

        result = ak.add_ask("q1", tags=["domain:test"], blocks=[act], session_id="sess-1")

        assert "error" not in result
        assert isinstance(result["id"], int)

    def test_add_ask_succeeds_when_relay_subscribe_raises(self, temp_db, disable_embedding, monkeypatch):
        act = _make_activity()

        def _boom(labels, *, caller_session_id):
            raise RuntimeError("relay unreachable")

        monkeypatch.setattr(ak, "relay_subscribe", _boom)

        result = ak.add_ask("q1", tags=["domain:test"], blocks=[act], session_id="sess-1")

        assert "error" not in result
        assert isinstance(result["id"], int)

    def test_new_subscription_notifies_relay_runtime(self, temp_db, disable_embedding, monkeypatch):
        """新規購読（reused: False）が成立したら、登録済みRelayRuntimeの
        notify_reconfigure()が実際に呼ばれること。"""
        act = _make_activity()

        def _fake_relay_subscribe(labels, *, caller_session_id):
            return {"subscription_id": "sub-1", "reused": False}

        monkeypatch.setattr(ak, "relay_subscribe", _fake_relay_subscribe)
        runtime = MagicMock()
        monkeypatch.setattr(relay_runtime_module, "_relay_runtime", runtime)

        result = ak.add_ask("q1", tags=["domain:test"], blocks=[act], session_id="sess-1")

        assert "error" not in result
        runtime.notify_reconfigure.assert_called_once()

    def test_relay_subscribe_called_for_second_session_on_dedup(
        self, temp_db, disable_embedding, monkeypatch
    ):
        """同一質問を別々のsession_idで2回add_askし、dedupヒットした2回目
        （occurrence_count > 1）についても、relay_subscribeがask個体label
        （ask:{id}）かつその後発session_idで呼ばれること。"""
        act = _make_activity()
        calls = []

        def _fake_relay_subscribe(labels, *, caller_session_id):
            calls.append((labels, caller_session_id))
            return {"subscription_id": f"sub-{len(calls)}", "reused": False}

        monkeypatch.setattr(ak, "relay_subscribe", _fake_relay_subscribe)

        r1 = ak.add_ask("same question", tags=["domain:test"], blocks=[act], session_id="sess-1")
        r2 = ak.add_ask("same question", tags=["domain:test"], blocks=[act], session_id="sess-2")

        assert r2["id"] == r1["id"]
        assert r2["deduped"] is True
        assert r2["occurrence_count"] > 1

        assert len(calls) == 2
        assert calls[0] == ([f"ask:{r1['id']}"], "sess-1")
        assert calls[1] == ([f"ask:{r2['id']}"], "sess-2")


class TestGetAsks:
    def test_default_filters_to_open_status(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")
        ak.add_ask("q2", tags=["domain:test"], blocks=[act])

        result = ak.get_asks()

        assert result["total_count"] == 1
        assert result["asks"][0]["question"] == "q2"

    def test_status_none_returns_all(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")
        ak.add_ask("q2", tags=["domain:test"], blocks=[act])

        result = ak.get_asks(status=None)

        assert result["total_count"] == 2

    def test_blocking_activity_id_filter(self, temp_db):
        act1 = _make_activity("a1")
        act2 = _make_activity("a2")
        ak.add_ask("q1", tags=["domain:test"], blocks=[act1])
        ak.add_ask("q2", tags=["domain:test"], blocks=[act2])

        result = ak.get_asks(blocking_activity_id=act1)

        assert result["total_count"] == 1
        assert result["asks"][0]["question"] == "q1"

    def test_triage_pending_only(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")
        ak.add_ask("q2", tags=["domain:test"], blocks=[act])

        result = ak.get_asks(triage_pending_only=True)

        assert result["total_count"] == 1
        assert result["asks"][0]["question"] == "q1"

    def test_invalid_status_rejected(self, temp_db):
        result = ak.get_asks(status="not_a_status")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_kind_rejected(self, temp_db):
        result = ak.get_asks(kind="not_a_kind")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_kind_filter(self, temp_db):
        act = _make_activity()
        ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.add_ask("q2", tags=["domain:test", "meta-ask"], blocks=[act], kind="meta")

        result = ak.get_asks(status=None, kind="meta")

        assert result["total_count"] == 1
        assert result["asks"][0]["question"] == "q2"

    def test_tags_filter_and_combination(self, temp_db):
        """複数タグ指定時はAND結合で絞り込む。"""
        act = _make_activity()
        ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.add_ask("q2", tags=["domain:test", "urgent"], blocks=[act])
        ak.add_ask("q3", tags=["domain:other", "urgent"], blocks=[act])

        result = ak.get_asks(status=None, tags=["domain:test", "urgent"])

        assert result["total_count"] == 1
        assert result["asks"][0]["question"] == "q2"

    def test_tags_filter_no_match_returns_empty(self, temp_db):
        act = _make_activity()
        ak.add_ask("q1", tags=["domain:test"], blocks=[act])

        result = ak.get_asks(status=None, tags=["domain:nonexistent"])

        assert result == {"asks": [], "total_count": 0}

    def test_tags_filter_nonexistent_tag_with_include_stats_still_returns_stats(self, temp_db):
        """存在しないタグでの絞り込み（resolve_tag_idsが空を返す経路）でも
        include_stats=Trueならstatsが付与される（通常の0件応答と一貫させる）。"""
        act = _make_activity()
        ak.add_ask("q1", tags=["domain:test"], blocks=[act])

        result = ak.get_asks(status=None, tags=["domain:nonexistent"], include_stats=True)

        assert result["asks"] == []
        assert result["total_count"] == 0
        assert result["stats"]["by_status"]["open"] == 1

    def test_tags_filter_and_combination_no_ask_matches_with_include_stats_still_returns_stats(self, temp_db):
        """個々のタグは存在するが同一askがAND全条件を満たさない経路
        （matched_ask_idsが空になる経路）でもinclude_stats=Trueならstatsが付与される。"""
        act = _make_activity()
        ak.add_ask("q1", tags=["domain:test", "alpha"], blocks=[act])
        ak.add_ask("q2", tags=["domain:test", "beta"], blocks=[act])

        result = ak.get_asks(status=None, tags=["alpha", "beta"], include_stats=True)

        assert result["asks"] == []
        assert result["total_count"] == 0
        assert result["stats"]["by_status"]["open"] == 2

    def test_empty_tags_list_rejected(self, temp_db):
        """空配列を明示指定した場合はadd_askと同じくTAGS_REQUIREDエラーになる。"""
        result = ak.get_asks(tags=[])
        assert result["error"]["code"] == "TAGS_REQUIRED"

    def test_response_hides_fingerprint_and_uses_id_raw(self, temp_db):
        act = _make_activity()
        ak.add_ask("q1", tags=["domain:test"], blocks=[act])

        result = ak.get_asks()
        ask = result["asks"][0]
        assert "fingerprint" not in ask
        assert "id" not in ask
        assert isinstance(ask["id_raw"], int)

    def test_promoted_decision_id_uses_id_raw(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")
        promoted = ak.triage_ask(r1["id"], action="promote", decision="d", reason="r")

        result = ak.get_asks(status="promoted")
        ask = result["asks"][0]
        assert "promoted_decision_id" not in ask
        assert ask["promoted_decision_id_raw"] == promoted["promoted_decision_id"]

    def test_include_stats(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")
        ak.add_ask("q2", tags=["domain:test"], blocks=[act])

        result = ak.get_asks(status=None, include_stats=True)

        assert result["stats"]["by_status"]["answered"] == 1
        assert result["stats"]["by_status"]["open"] == 1
        assert result["stats"]["last_30d"] == 2

    def test_limit_over_max_is_clamped_and_actually_restricts_rows(self, temp_db, disable_embedding):
        act = _make_activity()
        total_rows = ak._MAX_LIMIT + 5
        for i in range(total_rows):
            ak.add_ask(f"question {i}", tags=["domain:test"], blocks=[act])

        result = ak.get_asks(status=None, limit=ak._MAX_LIMIT + 1)

        assert result["total_count"] == total_rows
        assert len(result["asks"]) == ak._MAX_LIMIT

    def test_offset_returns_next_page_in_last_seen_desc_id_desc_order(self, temp_db):
        act = _make_activity()
        ids = [ak.add_ask(f"question {i}", tags=["domain:test"], blocks=[act])["id"] for i in range(5)]
        # デフォルトソートは last_seen_at DESC, id DESC のため、投稿順の逆順になる
        expected_order = list(reversed(ids))

        page1 = ak.get_asks(status=None, limit=2, offset=0)
        page2 = ak.get_asks(status=None, limit=2, offset=2)
        page3 = ak.get_asks(status=None, limit=2, offset=4)

        assert [a["id_raw"] for a in page1["asks"]] == expected_order[0:2]
        assert [a["id_raw"] for a in page2["asks"]] == expected_order[2:4]
        assert [a["id_raw"] for a in page3["asks"]] == expected_order[4:5]
        assert page1["total_count"] == page2["total_count"] == page3["total_count"] == 5


class TestAnswerAsk:
    def test_success_transitions_to_answered(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])

        result = ak.answer_ask(r1["id"], "my answer")

        assert result["status"] == "answered"
        assert result["triage_pending"] is True
        assert result["blocked_activities"] == [act]

    def test_empty_answer_body_rejected(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])

        result = ak.answer_ask(r1["id"], "   ")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_answer_body_over_max_len_rejected(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])

        result = ak.answer_ask(r1["id"], "x" * (ak.ANSWER_BODY_MAX_LEN + 1))
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_reanswer_rejected_toctou_safe(self, temp_db):
        """1段クエリUPDATEのrowcountチェックで、既にanswered状態への再answerを拒否する。"""
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "first answer")

        result = ak.answer_ask(r1["id"], "second answer")

        assert result["error"]["code"] == "VALIDATION_ERROR"
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT answer_body FROM asks WHERE id = ?", (r1["id"],)
            ).fetchone()
        finally:
            conn.close()
        assert row["answer_body"] == "first answer"

    def test_answer_nonexistent_ask_rejected(self, temp_db):
        result = ak.answer_ask(999999, "answer")
        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestTriageAsk:
    def test_promote_strips_decision_and_reason_before_saving(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")

        result = ak.triage_ask(
            r1["id"], action="promote", decision="  do X  ", reason="  because Y  ",
        )

        conn = get_connection()
        try:
            dec_row = conn.execute(
                "SELECT decision, reason FROM decisions WHERE id = ?",
                (result["promoted_decision_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert dec_row["decision"] == "do X"
        assert dec_row["reason"] == "because Y"

    def test_dismiss_strips_reason_before_saving(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")

        ak.triage_ask(r1["id"], action="dismiss", dismiss_reason="  not needed  ")

        listed = ak.get_asks(status="dismissed")
        assert listed["asks"][0]["triage_reason"] == "not needed"

    def test_promote_creates_decision_and_links_it(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")

        result = ak.triage_ask(
            r1["id"], action="promote", decision="do X", reason="because Y",
            title="t", tags=["domain:test"],
        )

        assert result["status"] == "promoted"
        assert isinstance(result["promoted_decision_id"], int)

        conn = get_connection()
        try:
            dec_row = conn.execute(
                "SELECT decision, reason FROM decisions WHERE id = ?",
                (result["promoted_decision_id"],),
            ).fetchone()
            blocks_count = conn.execute(
                "SELECT COUNT(*) FROM ask_blocks WHERE ask_id = ?", (r1["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert dec_row["decision"] == "do X"
        assert blocks_count == 0

    def test_dismiss_retains_answer_body(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")

        result = ak.triage_ask(r1["id"], action="dismiss", dismiss_reason="not needed")

        assert result["status"] == "dismissed"
        listed = ak.get_asks(status="dismissed")
        assert listed["asks"][0]["answer_body"] == "a1"
        assert listed["asks"][0]["triage_reason"] == "not needed"

    def test_invalid_action_rejected(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")

        result = ak.triage_ask(r1["id"], action="not_an_action")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_promote_missing_decision_rejected(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")

        result = ak.triage_ask(r1["id"], action="promote", reason="r")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_dismiss_missing_reason_rejected(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")

        result = ak.triage_ask(r1["id"], action="dismiss")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_triage_on_open_ask_rejected(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])

        result = ak.triage_ask(r1["id"], action="dismiss", dismiss_reason="x")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_double_triage_rejected_toctou_safe(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")
        ak.triage_ask(r1["id"], action="dismiss", dismiss_reason="first")

        result = ak.triage_ask(r1["id"], action="dismiss", dismiss_reason="second")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_promote_rolls_back_ask_state_on_decision_failure(self, temp_db, monkeypatch):
        """decision_service例外時、SAVEPOINTロールバックでask側は'answered'に戻る。"""
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")

        def _boom(items):
            raise RuntimeError("decision service exploded")

        monkeypatch.setattr(ak, "add_decisions", _boom)

        result = ak.triage_ask(r1["id"], action="promote", decision="d", reason="r")

        assert result["error"]["code"] == "DATABASE_ERROR"
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT status, triage FROM asks WHERE id = ?", (r1["id"],)
            ).fetchone()
            blocks_count = conn.execute(
                "SELECT COUNT(*) FROM ask_blocks WHERE ask_id = ?", (r1["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert row["status"] == "answered"
        assert row["triage"] is None
        assert blocks_count == 1

    def test_triage_succeeds_even_if_blocked_activity_already_completed(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")
        update_activity(act, status="completed")

        result = ak.triage_ask(r1["id"], action="dismiss", dismiss_reason="x")
        assert result["status"] == "dismissed"


class TestWithdrawAsk:
    def test_success_transitions_to_withdrawn(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])

        result = ak.withdraw_ask(r1["id"], "posted by mistake")

        assert result["status"] == "withdrawn"

    def test_withdraw_removes_ask_blocks_but_keeps_requesters(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act], session_id="sess-1")

        ak.withdraw_ask(r1["id"], "posted by mistake")

        conn = get_connection()
        try:
            blocks_count = conn.execute(
                "SELECT COUNT(*) FROM ask_blocks WHERE ask_id = ?", (r1["id"],)
            ).fetchone()[0]
            requesters_count = conn.execute(
                "SELECT COUNT(*) FROM ask_requesters WHERE ask_id = ?", (r1["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert blocks_count == 0
        assert requesters_count == 1

    def test_withdraw_non_open_rejected(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")

        result = ak.withdraw_ask(r1["id"], "posted by mistake")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_empty_reason_rejected(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])

        result = ak.withdraw_ask(r1["id"], "   ")
        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestGetPendingAsksWithConn:
    def test_open_ask_is_awaiting_answer(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])

        conn = get_connection()
        try:
            pending = ak.get_pending_asks_with_conn(conn, act)
        finally:
            conn.close()

        assert pending["awaiting_answer"][0]["id_raw"] == r1["id"]
        assert pending["awaiting_triage"] == []

    def test_answered_ask_is_awaiting_triage_with_answer_body(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "my answer")

        conn = get_connection()
        try:
            pending = ak.get_pending_asks_with_conn(conn, act)
        finally:
            conn.close()

        assert pending["awaiting_answer"] == []
        assert pending["awaiting_triage"][0]["answer_body"] == "my answer"

    def test_promoted_ask_is_not_delivered(self, temp_db):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "a1")
        ak.triage_ask(r1["id"], action="dismiss", dismiss_reason="x")

        conn = get_connection()
        try:
            pending = ak.get_pending_asks_with_conn(conn, act)
        finally:
            conn.close()

        assert pending["awaiting_answer"] == []
        assert pending["awaiting_triage"] == []

    def test_completed_activity_does_not_deliver_pending_asks(self, temp_db):
        act = _make_activity()
        ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        update_activity(act, status="completed")

        conn = get_connection()
        try:
            pending = ak.get_pending_asks_with_conn(conn, act)
        finally:
            conn.close()

        assert pending["awaiting_answer"] == []


class TestSimilarSuggestions:
    """add_askのsimilar_precedents/similar_asksはembeddingサーバー未起動時に
    空配列を返すfail-safe設計であることを検証する（実サーバー接続はmockで代替）。"""

    def test_empty_when_embedding_server_unavailable(self, temp_db, disable_embedding):
        act = _make_activity()
        result = ak.add_ask("q1", tags=["domain:test"], blocks=[act])

        assert result["similar_precedents"] == []
        assert result["similar_asks"] == []

    def test_similar_asks_populated_when_embedding_available(self, temp_db, monkeypatch):
        import src.services.embedding_service as emb

        def mock_encode_batch(texts, prefix):
            return [[0.001] * 384 for _ in texts]

        monkeypatch.setattr(emb, "_encode_batch", mock_encode_batch)
        monkeypatch.setattr(emb, "_server_initialized", True)
        monkeypatch.setattr(emb, "_backfill_done", True)

        act = _make_activity()
        r1 = ak.add_ask("first question", tags=["domain:test"], blocks=[act])
        assert r1["similar_asks"] == []

        r2 = ak.add_ask("second question", tags=["domain:test"], blocks=[act])
        assert any(item["id"] == r1["id"] for item in r2["similar_asks"])
