"""tag notesの配信済み帳簿のテスト

- notesが書き換わったら、配信済みのセッションへ1回だけ全文を配り直す
- 読み取り経路（mark=False）は専用の帳簿を持ち、両方の帳簿を見て自分の帳簿にだけ書く
- 天井で畳んだタグはどちらの帳簿にも入らず、ポインタはnotesの版ごとに1回出る
- 書き換えた本人には戻らない・圧縮等で帳簿を消すと全文が届き直す
"""
import ast
from pathlib import Path

import pytest

import src.services.embedding_service as emb
from src.db import get_connection
from src.infra import session_identity
from src.services import tag_service
from src.services.tag_service import (
    _get_delivered_tags,
    _injected_tags,
    collect_tag_notes_for_injection,
    release_folded_tag,
    reset_delivery,
    update_tag,
)
from src.services.topic_service import add_topic

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    monkeypatch.setattr(emb, "_server_initialized", False)
    monkeypatch.setattr(emb, "_backfill_done", True)
    monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)


@pytest.fixture
def conn(temp_db):
    add_topic(title="Test", description="Desc", tags=["domain:a", "domain:b"])
    update_tag("domain:a", "Aの教訓")
    update_tag("domain:b", "Bの教訓")
    c = get_connection()
    _set_notes(c, "domain:a", "Aの教訓", "2026-01-01 00:00:00")
    _set_notes(c, "domain:b", "Bの教訓", "2026-01-01 00:00:00")
    yield c
    c.close()


def _set_notes(conn, tag: str, notes: str, notes_updated_at: str | None) -> None:
    ns, name = tag.split(":")
    conn.execute(
        "UPDATE tags SET notes = ?, notes_updated_at = ? WHERE namespace = ? AND name = ?",
        (notes, notes_updated_at, ns, name),
    )
    conn.commit()


def _tags(result) -> list[str]:
    return [r["tag"] for r in result or []]


def _get(conn, tags, session_id="sess-1"):
    return collect_tag_notes_for_injection(conn, tags, session_id=session_id, mark=False)


def _mark(conn, tags, session_id="sess-1"):
    return collect_tag_notes_for_injection(conn, tags, session_id=session_id)


class TestRedeliverOnUpdate:
    def test_updated_notes_are_redelivered_once(self, conn):
        assert _tags(_mark(conn, ["domain:a"])) == ["domain:a"]
        _set_notes(conn, "domain:a", "Aの教訓・改訂", "2026-02-01 00:00:00")

        assert _mark(conn, ["domain:a"]) == [{"tag": "domain:a", "notes": "Aの教訓・改訂"}]
        assert _mark(conn, ["domain:a"]) is None

    def test_not_redelivered_without_update(self, conn):
        _mark(conn, ["domain:a"])
        assert _mark(conn, ["domain:a"]) is None
        assert _get(conn, ["domain:a"]) is None

    def test_null_notes_updated_at_is_delivered_once(self, conn):
        _set_notes(conn, "domain:a", "Aの教訓", None)

        assert _tags(_mark(conn, ["domain:a"])) == ["domain:a"]
        assert _mark(conn, ["domain:a"]) is None
        assert _injected_tags["sess-1"]["domain:a"] == ""

    def test_tag_only_in_get_ledger_is_redelivered_by_get_once(self, conn):
        assert _tags(_get(conn, ["domain:a"])) == ["domain:a"]
        assert "domain:a" not in _injected_tags.get("sess-1", {})
        _set_notes(conn, "domain:a", "Aの教訓・改訂", "2026-02-01 00:00:00")

        assert _get(conn, ["domain:a"]) == [{"tag": "domain:a", "notes": "Aの教訓・改訂"}]
        assert _get(conn, ["domain:a"]) is None

    def test_decayed_tag_gets_full_text_once_after_update(self, conn):
        conn.execute(
            "UPDATE tags SET created_at = datetime('now', '-400 days'), last_injected_at = NULL, "
            "notes_updated_at = datetime('now', '-400 days') WHERE namespace = 'domain' AND name = 'a'"
        )
        conn.commit()
        pointer = _mark(conn, ["domain:a"])
        assert "Aの教訓" not in pointer[0]["notes"]
        assert _mark(conn, ["domain:a"]) is None
        assert _get(conn, ["domain:a"]) is None

        conn.execute(
            "UPDATE tags SET notes_updated_at = CURRENT_TIMESTAMP WHERE namespace = 'domain' AND name = 'a'"
        )
        conn.commit()
        assert _mark(conn, ["domain:a"]) == [{"tag": "domain:a", "notes": "Aの教訓"}]
        assert _mark(conn, ["domain:a"]) is None


class TestReadPathLedger:
    def test_get_after_get_is_not_repeated(self, conn):
        assert _tags(_get(conn, ["domain:a"])) == ["domain:a"]
        assert _get(conn, ["domain:a", "domain:b"]) == [{"tag": "domain:b", "notes": "Bの教訓"}]
        assert _get(conn, ["domain:a", "domain:b"]) is None

    def test_get_after_mark_true_is_not_repeated(self, conn):
        _mark(conn, ["domain:a"])
        assert _get(conn, ["domain:a"]) is None

    def test_mark_true_after_get_still_delivers_once(self, conn):
        _get(conn, ["domain:a"])
        assert _tags(_mark(conn, ["domain:a"])) == ["domain:a"]
        assert _mark(conn, ["domain:a"]) is None
        assert _get(conn, ["domain:a"]) is None

    def test_folded_tag_is_delivered_by_get_once(self, conn):
        _mark(conn, ["domain:a"])
        assert release_folded_tag("sess-1", "domain:a") is True

        assert _tags(_get(conn, ["domain:a"])) == ["domain:a"]
        assert _get(conn, ["domain:a"]) is None

    def test_subagent_get_does_not_read_or_write_parent_ledgers(self, conn):
        _mark(conn, ["domain:a"])
        _get(conn, ["domain:b"])
        parent_marked = dict(_injected_tags["sess-1"])
        parent_got = dict(_get_delivered_tags["sess-1"])

        token = session_identity.set_current_agent_id("agent-1")
        try:
            assert _tags(_get(conn, ["domain:a", "domain:b"])) == ["domain:a", "domain:b"]
            assert _get(conn, ["domain:a", "domain:b"]) is None
        finally:
            session_identity.reset_current_agent_id(token)

        assert _injected_tags["sess-1"] == parent_marked
        assert _get_delivered_tags["sess-1"] == parent_got
        assert set(_get_delivered_tags["sess-1#agent-1"]) == {"domain:a", "domain:b"}

    def test_unidentified_caller_is_not_recorded(self, conn):
        for _ in range(2):
            assert _tags(_get(conn, ["domain:a"], session_id=None)) == ["domain:a"]
            assert _tags(_mark(conn, ["domain:a"], session_id=None)) == ["domain:a"]
        assert _injected_tags == {}
        assert _get_delivered_tags == {}


class TestFoldPointerVersion:
    def test_new_pointer_is_shown_once_after_update(self, conn):
        _mark(conn, ["domain:a"])
        assert release_folded_tag("sess-1", "domain:a") is True
        _mark(conn, ["domain:a"])
        assert release_folded_tag("sess-1", "domain:a") is False

        _set_notes(conn, "domain:a", "Aの教訓・改訂", "2026-02-01 00:00:00")
        _mark(conn, ["domain:a"])
        assert release_folded_tag("sess-1", "domain:a") is True
        _mark(conn, ["domain:a"])
        assert release_folded_tag("sess-1", "domain:a") is False

    def test_folded_tag_is_in_neither_ledger(self, conn):
        _mark(conn, ["domain:a"])
        release_folded_tag("sess-1", "domain:a")
        assert "domain:a" not in _injected_tags["sess-1"]
        assert "domain:a" not in _get_delivered_tags.get("sess-1", {})


class TestOwnUpdate:
    def test_writer_does_not_get_own_update_back(self, conn, monkeypatch):
        _mark(conn, ["domain:a"], session_id="writer")
        _mark(conn, ["domain:a"], session_id="reader")
        _get(conn, ["domain:b"], session_id="writer")
        _get(conn, ["domain:b"], session_id="reader")

        monkeypatch.setattr(tag_service, "get_caller_session_id", lambda: "writer")
        update_tag("domain:a", "Aの教訓・改訂")
        update_tag("domain:b", "Bの教訓・改訂")

        assert _mark(conn, ["domain:a"], session_id="writer") is None
        assert _get(conn, ["domain:b"], session_id="writer") is None
        assert _tags(_mark(conn, ["domain:a"], session_id="reader")) == ["domain:a"]
        assert _tags(_get(conn, ["domain:b"], session_id="reader")) == ["domain:b"]

    def test_writer_still_receives_notes_never_delivered(self, conn, monkeypatch):
        monkeypatch.setattr(tag_service, "get_caller_session_id", lambda: "writer")
        update_tag("domain:a", "Aの教訓・改訂")
        assert _tags(_mark(conn, ["domain:a"], session_id="writer")) == ["domain:a"]

    def test_subagent_update_is_redelivered_to_parent_once(self, conn, monkeypatch):
        _mark(conn, ["domain:a"])
        monkeypatch.setattr(tag_service, "get_caller_session_id", lambda: "sess-1")
        token = session_identity.set_current_agent_id("agent-1")
        try:
            update_tag("domain:a", "Aの教訓・改訂")
        finally:
            session_identity.reset_current_agent_id(token)

        assert _mark(conn, ["domain:a"]) == [{"tag": "domain:a", "notes": "Aの教訓・改訂"}]
        assert _mark(conn, ["domain:a"]) is None


class TestResetDelivery:
    def test_reset_redelivers_full_text_for_session_and_subagents(self, conn):
        _mark(conn, ["domain:a"])
        _get(conn, ["domain:b"])
        release_folded_tag("sess-1", "domain:a")
        token = session_identity.set_current_agent_id("agent-1")
        try:
            _mark(conn, ["domain:a"])
        finally:
            session_identity.reset_current_agent_id(token)
        _mark(conn, ["domain:a"], session_id="other")

        assert reset_delivery("sess-1") == 4

        assert _tags(_get(conn, ["domain:a", "domain:b"])) == ["domain:a", "domain:b"]
        assert _tags(_mark(conn, ["domain:a", "domain:b"])) == ["domain:a", "domain:b"]
        assert release_folded_tag("sess-1", "domain:a") is True
        assert _mark(conn, ["domain:a"], session_id="other") is None


def test_read_path_call_sites_are_the_five_get_tools():
    """mark=Falseで注入する呼び出し元を固定する（増減したら帳簿の扱いを見直す）。"""
    tree = ast.parse((_REPO_ROOT / "src" / "main.py").read_text(encoding="utf-8"))
    callers = set()
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_maybe_inject_tag_notes"
                and any(kw.arg == "mark" and getattr(kw.value, "value", None) is False for kw in node.keywords)
            ):
                callers.add(func.name)
    assert callers == {"get_topics", "get_logs", "get_decisions", "pull_precedents", "get_activities"}


class TestSessionStartReset:
    @pytest.mark.parametrize("source,expected", [("compact", 1), ("clear", 1), ("resume", 1), ("startup", 0), (None, 0)])
    def test_resets_only_when_context_is_replaced(self, monkeypatch, source, expected):
        from hooks import session_start_hook
        from src.infra import loopback_http

        sent = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(request, timeout):
            sent.append(request)
            return _Resp()

        class _Harness:
            def resolve_session_identity(self):
                return "bridge-1"

        monkeypatch.setattr(loopback_http.NO_PROXY_OPENER, "open", fake_open)
        session_start_hook._reset_tag_notes_delivery(_Harness(), source)

        assert len(sent) == expected
        if sent:
            assert sent[0].full_url.endswith("/session/reset-delivery")
            assert sent[0].data == b'{"session_id": "bridge-1"}'

    def test_server_unreachable_does_not_raise(self, monkeypatch):
        from hooks import session_start_hook
        from src.infra import loopback_http

        def fail(request, timeout):
            raise OSError("refused")

        class _Harness:
            def resolve_session_identity(self):
                return "bridge-1"

        monkeypatch.setattr(loopback_http.NO_PROXY_OPENER, "open", fail)
        session_start_hook._reset_tag_notes_delivery(_Harness(), "compact")
