"""relay/server.py のユニットテスト。

カバー範囲:
- _broadcast: 送信者と同一handleの購読者はブロードキャスト対象外（test_case17相当）
- _broadcast: SSE notifyペイロードに msg_id / body / handle / created_at の4フィールドが添付される
- init_db: idx_messages_channel_msg_id インデックスが作成され、再呼び出しでも維持される
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
        """送信者と同じhandleを持つ複数subscriberが全員除外される。"""
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

    def test_payload_contains_expected_fields(self, channel):
        """msg_id / body / handle / created_at の4フィールドが正しく含まれる。"""
        body_content = '{"v":1,"kind":"event","data":{"type":"state"}}'
        q: queue.Queue = queue.Queue()
        with srv._sub_lock:
            srv._subscribers[channel] = [("bob", q)]

        srv._broadcast(
            channel,
            "alice",
            _make_msg(channel, handle="alice", body=body_content, msg_id=42),
        )
        payload = json.loads(q.get(timeout=1))

        assert payload["msg_id"] == 42
        assert payload["body"] == body_content
        assert payload["handle"] == "alice"
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

    def test_init_db_idempotent(self, tmp_path):
        """init_db を2回呼んでもエラーが起きず、インデックスが維持される。"""
        db_path = str(tmp_path / "relay.db")
        srv.init_db(db_path)
        srv.init_db(db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_messages_channel_msg_id'"
        ).fetchone()
        conn.close()
        assert row is not None, "idx_messages_channel_msg_id が2回目init_db後に失われた"
