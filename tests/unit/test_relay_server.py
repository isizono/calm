"""relay/server.py のユニットテスト。

カバー範囲:
- _broadcast: 送信者と同一handleの購読者はブロードキャスト対象外（test_case17相当）
- _broadcast: SSE notifyペイロードに msg_id / body / handle / created_at の4フィールドが添付される
- init_db: idx_messages_channel_msg_id / idx_messages_channel_handle_msg インデックスが作成される
"""
import json
import queue
import sqlite3

import pytest

import src.relay.server as srv


@pytest.fixture
def tmp_db(tmp_path):
    db_path = str(tmp_path / "relay.db")
    srv.init_db(db_path)
    return db_path


@pytest.fixture
def channel(tmp_db):
    code = srv.create_channel(tmp_db)
    yield code
    with srv._sub_lock:
        srv._subscribers.pop(code, None)


def _make_msg(channel_code, handle="alice", body='{"v":1}', msg_id=1):
    return {
        "msg_id": msg_id,
        "channel_code": channel_code,
        "handle": handle,
        "body": body,
        "needs_reply": False,
        "in_reply_to": None,
        "created_at": "2026-06-14T10:00:00+00:00",
    }


class TestBroadcastSenderExcluded:
    """送信者と同一handleの購読者はブロードキャスト対象外（test_case17相当）。"""

    def test_sender_handle_excluded_from_broadcast(self, channel):
        """送信者alice自身のqueueにはメッセージが届かず、別ユーザーbobのqueueには届く。"""
        sender_q: queue.Queue = queue.Queue()
        other_q: queue.Queue = queue.Queue()

        with srv._sub_lock:
            srv._subscribers[channel] = [
                ("alice", sender_q),
                ("bob", other_q),
            ]

        srv._broadcast(channel, "alice", _make_msg(channel))

        assert not other_q.empty(), "bob にメッセージが届いていない"
        assert sender_q.empty(), "送信者 alice 自身にエコーされた"

    def test_multiple_same_handle_all_excluded(self, channel):
        """送信者と同じhandleを持つ複数purchaserが全員除外される。"""
        alice_q1: queue.Queue = queue.Queue()
        alice_q2: queue.Queue = queue.Queue()
        bob_q: queue.Queue = queue.Queue()

        with srv._sub_lock:
            srv._subscribers[channel] = [
                ("alice", alice_q1),
                ("alice", alice_q2),
                ("bob", bob_q),
            ]

        srv._broadcast(channel, "alice", _make_msg(channel))

        assert alice_q1.empty(), "alice の1つ目のqueueにエコーされた"
        assert alice_q2.empty(), "alice の2つ目のqueueにエコーされた"
        assert not bob_q.empty(), "bob にメッセージが届いていない"


class TestBroadcastPayloadBody:
    """SSE notifyペイロードにbody本体4フィールドが添付される。"""

    def test_payload_contains_msg_id(self, channel):
        """ペイロードにmsg_idが含まれる。"""
        q: queue.Queue = queue.Queue()
        with srv._sub_lock:
            srv._subscribers[channel] = [("bob", q)]

        srv._broadcast(channel, "alice", _make_msg(channel, msg_id=42))
        payload = json.loads(q.get(timeout=1))

        assert payload["msg_id"] == 42

    def test_payload_contains_body(self, channel):
        """ペイロードにbody（JSON文字列）が含まれる。"""
        body_content = '{"v":1,"kind":"event","data":{"type":"state"}}'
        q: queue.Queue = queue.Queue()
        with srv._sub_lock:
            srv._subscribers[channel] = [("bob", q)]

        srv._broadcast(channel, "alice", _make_msg(channel, body=body_content))
        payload = json.loads(q.get(timeout=1))

        assert payload["body"] == body_content

    def test_payload_contains_handle(self, channel):
        """ペイロードにhandleが含まれる。"""
        q: queue.Queue = queue.Queue()
        with srv._sub_lock:
            srv._subscribers[channel] = [("bob", q)]

        srv._broadcast(channel, "alice", _make_msg(channel, handle="alice"))
        payload = json.loads(q.get(timeout=1))

        assert payload["handle"] == "alice"

    def test_payload_contains_created_at(self, channel):
        """ペイロードにcreated_atが含まれる。"""
        q: queue.Queue = queue.Queue()
        with srv._sub_lock:
            srv._subscribers[channel] = [("bob", q)]

        msg = _make_msg(channel)
        srv._broadcast(channel, "alice", msg)
        payload = json.loads(q.get(timeout=1))

        assert payload["created_at"] == "2026-06-14T10:00:00+00:00"

    def test_payload_excludes_extra_fields(self, channel):
        """ペイロードにchannel_code/needs_reply/in_reply_toは含まれない。"""
        q: queue.Queue = queue.Queue()
        with srv._sub_lock:
            srv._subscribers[channel] = [("bob", q)]

        srv._broadcast(channel, "alice", _make_msg(channel))
        payload = json.loads(q.get(timeout=1))

        assert "channel_code" not in payload
        assert "needs_reply" not in payload
        assert "in_reply_to" not in payload


class TestInitDbIndexes:
    """init_db でインデックスが作成される（非破壊改修）。"""

    def test_creates_channel_msg_id_index(self, tmp_path):
        """idx_messages_channel_msg_id インデックスが作成される。"""
        db_path = str(tmp_path / "relay.db")
        srv.init_db(db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_messages_channel_msg_id'"
        ).fetchone()
        conn.close()
        assert row is not None, "idx_messages_channel_msg_id が作成されていない"

    def test_creates_channel_handle_msg_index(self, tmp_path):
        """idx_messages_channel_handle_msg インデックスが作成される。"""
        db_path = str(tmp_path / "relay.db")
        srv.init_db(db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_messages_channel_handle_msg'"
        ).fetchone()
        conn.close()
        assert row is not None, "idx_messages_channel_handle_msg が作成されていない"

    def test_init_db_idempotent(self, tmp_path):
        """init_db を2回呼んでも CREATE INDEX IF NOT EXISTS でエラーが起きない。"""
        db_path = str(tmp_path / "relay.db")
        srv.init_db(db_path)
        srv.init_db(db_path)


class TestParseKeepaliveSec:
    """_parse_keepalive_sec の不正値バリデーション。"""

    def test_none_returns_default(self):
        assert srv._parse_keepalive_sec(None) == 10

    def test_empty_string_returns_default(self):
        assert srv._parse_keepalive_sec("") == 10

    def test_positive_integer(self):
        assert srv._parse_keepalive_sec("5") == 5

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            srv._parse_keepalive_sec("0")

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            srv._parse_keepalive_sec("-1")

    def test_non_integer_raises(self):
        with pytest.raises(ValueError, match="must be a positive integer"):
            srv._parse_keepalive_sec("10.5")

    def test_garbage_raises(self):
        with pytest.raises(ValueError, match="must be a positive integer"):
            srv._parse_keepalive_sec("abc")
