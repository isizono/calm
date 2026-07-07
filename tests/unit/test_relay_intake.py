"""B-1 intake の frame 振り分け・ack tracker・SSE consume の unit test。

frame parse・所有 session 逆引き・inbox 振り分け・ack tracker の各挙動を純ロジック
としてカバーする（SSE 接続の実配線は integration 側で検証する）。
"""
from __future__ import annotations

import httpx
import pytest

from src.services.relay import declarations, inbox, intake
from src.services.relay.intake import (
    AckTracker,
    DispatchResult,
    StreamFrame,
    SubFrame,
    dispatch_frame,
    parse_frame,
    resolve_stream_targets,
    resolve_sub_owner,
)


@pytest.fixture(autouse=True)
def relay_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("RELAY_IDENTITY", raising=False)


def _write_declaration(session_id: str, subscriptions: list[dict]) -> None:
    decl = declarations.ensure(session_id)
    decl["subscriptions"] = subscriptions
    declarations.save(decl)


# ---------------------------------------------------------------------------
# parse_frame
# ---------------------------------------------------------------------------


class TestParseFrame:
    def test_sub_frame(self):
        frame = parse_frame({"delivery_target": "sub:s-1", "publish_id": 3, "ref": {}})
        assert isinstance(frame, SubFrame)
        assert frame.subscription_id == "s-1"
        assert frame.publish_id == 3

    def test_stream_frame(self):
        frame = parse_frame(
            {
                "delivery_target": "stream:cc-memory:planning",
                "publish_id": 7,
                "body": "hi",
            }
        )
        assert isinstance(frame, StreamFrame)
        assert frame.stream_id == "cc-memory:planning"
        assert frame.stream_name == "planning"
        assert frame.publish_id == 7

    @pytest.mark.parametrize(
        "data",
        [
            {},
            {"delivery_target": None, "publish_id": 1},
            {"delivery_target": "sub:", "publish_id": 1},
            {"delivery_target": "stream:onlyname", "publish_id": 1},
            {"delivery_target": "sub:s-1"},  # publish_id なし
            {"delivery_target": "sub:s-1", "publish_id": True},  # bool は不可
            {"delivery_target": "other:x", "publish_id": 1},
        ],
    )
    def test_invalid_frames_return_none(self, data):
        assert parse_frame(data) is None


# ---------------------------------------------------------------------------
# 逆引き
# ---------------------------------------------------------------------------


class TestResolvers:
    def test_sub_owner_lookup_only_declared(self, tmp_path):
        _write_declaration(
            "sess-a", [{"subscription_id": "s-1", "labels": ["x"]}]
        )
        _write_declaration(
            "sess-b", [{"subscription_id": "s-2", "labels": ["y"]}]
        )
        snapshot = declarations.load_all()
        assert resolve_sub_owner(snapshot, "s-1") == "sess-a"
        assert resolve_sub_owner(snapshot, "s-2") == "sess-b"
        assert resolve_sub_owner(snapshot, "s-3") is None

    def test_stream_targets_return_all_declaring_sessions(self):
        _write_declaration(
            "sess-a",
            [{"subscription_id": "s-1", "labels": ["room:planning"]}],
        )
        _write_declaration(
            "sess-b",
            [{"subscription_id": "s-2", "labels": ["room:planning", "other"]}],
        )
        _write_declaration(
            "sess-c",
            [{"subscription_id": "s-3", "labels": ["room:release"]}],
        )
        snapshot = declarations.load_all()
        assert set(resolve_stream_targets(snapshot, "planning")) == {"sess-a", "sess-b"}
        assert resolve_stream_targets(snapshot, "release") == ["sess-c"]
        assert resolve_stream_targets(snapshot, "nobody") == []


# ---------------------------------------------------------------------------
# dispatch_frame
# ---------------------------------------------------------------------------


class TestDispatchSubFrame:
    def test_sub_frame_writes_only_to_owner_inbox(self):
        _write_declaration(
            "sess-a", [{"subscription_id": "s-1", "labels": ["x"]}]
        )
        _write_declaration(
            "sess-b", [{"subscription_id": "s-2", "labels": ["x"]}]
        )
        snapshot = declarations.load_all()
        tracker = AckTracker()
        frame = SubFrame(subscription_id="s-1", publish_id=5, payload={"n": 1})
        result = dispatch_frame(frame, snapshot, tracker)
        assert result.written_sessions == ["sess-a"]
        assert tracker.sub_pending == {"s-1": 5}
        assert [m for m in inbox.drain("sess-a")] == [{"n": 1}]
        assert inbox.drain("sess-b") == []

    def test_sub_frame_with_no_owner_is_dropped_without_ack(self):
        snapshot = declarations.load_all()  # 空
        tracker = AckTracker()
        frame = SubFrame(subscription_id="s-lost", publish_id=1, payload={})
        result = dispatch_frame(frame, snapshot, tracker)
        assert result.dropped is True
        assert tracker.sub_pending == {}

    def test_sub_frame_append_failure_does_not_ack(self, monkeypatch):
        _write_declaration(
            "sess-a", [{"subscription_id": "s-1", "labels": ["x"]}]
        )
        snapshot = declarations.load_all()
        tracker = AckTracker()

        def failing_append(session_id, record):
            raise OSError("disk full")

        result = dispatch_frame(
            SubFrame(subscription_id="s-1", publish_id=3, payload={}),
            snapshot,
            tracker,
            inbox_append=failing_append,
        )
        assert result.dropped is True
        assert tracker.sub_pending == {}

    def test_ack_tracker_takes_max_publish_id(self):
        _write_declaration("sess-a", [{"subscription_id": "s-1", "labels": ["x"]}])
        snapshot = declarations.load_all()
        tracker = AckTracker()
        for pid in [4, 2, 5, 3]:
            dispatch_frame(
                SubFrame(subscription_id="s-1", publish_id=pid, payload={"i": pid}),
                snapshot,
                tracker,
            )
        assert tracker.sub_pending == {"s-1": 5}


class TestDispatchStreamFrame:
    def test_stream_frame_fans_out_to_all_declaring_sessions(self):
        _write_declaration(
            "sess-a",
            [{"subscription_id": "s-1", "labels": ["room:planning"]}],
        )
        _write_declaration(
            "sess-b",
            [{"subscription_id": "s-2", "labels": ["room:planning"]}],
        )
        snapshot = declarations.load_all()
        tracker = AckTracker()
        frame = StreamFrame(
            stream_id="cc-memory:planning",
            stream_name="planning",
            publish_id=9,
            payload={"body": "hello"},
        )
        result = dispatch_frame(frame, snapshot, tracker)
        assert set(result.written_sessions) == {"sess-a", "sess-b"}
        assert tracker.stream_pending == {"cc-memory:planning": 9}
        assert inbox.drain("sess-a") == [{"body": "hello"}]
        assert inbox.drain("sess-b") == [{"body": "hello"}]

    def test_stream_frame_with_no_declarer_is_dropped_and_acked(self):
        snapshot = declarations.load_all()  # 空
        tracker = AckTracker()
        frame = StreamFrame(
            stream_id="cc-memory:planning",
            stream_name="planning",
            publish_id=1,
            payload={},
        )
        result = dispatch_frame(frame, snapshot, tracker)
        assert result.dropped is True
        assert result.reason == "no_stream_target"
        # 宣言ゼロは配達義務外 → outbox に残さないため ack は進める。
        assert tracker.stream_pending == {"cc-memory:planning": 1}

    def test_stream_frame_partial_write_failure_does_not_ack(self, monkeypatch):
        _write_declaration(
            "sess-a",
            [{"subscription_id": "s-1", "labels": ["room:planning"]}],
        )
        _write_declaration(
            "sess-b",
            [{"subscription_id": "s-2", "labels": ["room:planning"]}],
        )
        snapshot = declarations.load_all()
        tracker = AckTracker()

        def append_but_fail_b(session_id, record):
            if session_id == "sess-b":
                raise OSError("disk full")
            inbox.append(session_id, record)

        result = dispatch_frame(
            StreamFrame(
                stream_id="cc-memory:planning",
                stream_name="planning",
                publish_id=2,
                payload={"body": "x"},
            ),
            snapshot,
            tracker,
            inbox_append=append_but_fail_b,
        )
        # sess-a は書けたが sess-b が失敗 → 全体としては ack しない（次回再配達）。
        assert "sess-a" in result.written_sessions
        assert tracker.stream_pending == {}


# ---------------------------------------------------------------------------
# AckTracker.flush（httpx.MockTransport で実 http を模す）
# ---------------------------------------------------------------------------


class TestAckTrackerFlush:
    def _make_client(self, dispatcher) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(dispatcher), base_url="http://relay.test"
        )

    def test_flush_sub_and_stream_uses_expected_endpoints(self):
        recorded: list[tuple[str, str]] = []

        def dispatch(request: httpx.Request) -> httpx.Response:
            recorded.append((request.method, request.url.path))
            return httpx.Response(200, json={})

        tracker = AckTracker()
        tracker.mark_sub("s-1", 10)
        tracker.mark_stream("cc-memory:planning", 20)
        with self._make_client(dispatch) as client:
            tracker.flush(client)
        assert ("POST", "/subscriptions/s-1/ack") in recorded
        assert ("POST", "/streams/cc-memory:planning/ack") in recorded
        assert tracker.sub_pending == {}
        assert tracker.stream_pending == {}

    def test_flush_transient_error_keeps_pending(self):
        def dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"code": "TransientError"})

        tracker = AckTracker()
        tracker.mark_sub("s-1", 3)
        with self._make_client(dispatch) as client:
            tracker.flush(client)
        # 送信失敗時は pending を残す。
        assert tracker.sub_pending == {"s-1": 3}
