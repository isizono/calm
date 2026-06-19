"""ow_service reducer 4関数のユニットテスト。"""
import logging

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from src.services import ow_service


# ----------------------------
# テストヘルパー
# ----------------------------


def _make_msg(msg_id, handle, body, created_at=None):
    """テスト用リレーメッセージを生成する。"""
    return {
        "msg_id": msg_id,
        "handle": handle,
        "body": body,
        "created_at": created_at or "2026-06-14T10:00:00+00:00",
    }


def _event_body(data_type, data_payload, handle="w-h", to="orch"):
    """v:1 / kind:event の envelope を生成する。"""
    return {
        "v": 1,
        "kind": "event",
        "from": handle,
        "to": to,
        "data": {"type": data_type, **data_payload},
    }


def _make_history(messages):
    return {"messages": messages}


# ----------------------------
# TestParseOwEvent
# ----------------------------


class TestParseOwEvent:
    """_parse_ow_event のユニットテスト。"""

    def test_valid_event_returns_parsed(self):
        """v=1, kind="event" → 正常にparsed dictを返す。"""
        body = _event_body("identity", {"alias": "w-1"})
        msg = _make_msg(42, "w-h", body, "2026-06-14T10:00:00+00:00")
        result = ow_service._parse_ow_event(msg)
        assert result is not None
        assert result["msg_id"] == 42
        assert result["handle"] == "w-h"
        assert result["body"] == body
        assert result["created_at"] == "2026-06-14T10:00:00+00:00"

    def test_v_mismatch_returns_none_with_warning(self, caplog):
        """v=2 → None を返し、warningログを出す。"""
        body = {"v": 2, "kind": "event", "data": {"type": "identity"}}
        msg = _make_msg(10, "w-h", body)
        with caplog.at_level(logging.WARNING, logger="src.services.ow_service"):
            result = ow_service._parse_ow_event(msg)
        assert result is None
        assert any("envelope v=" in r.message for r in caplog.records)

    def test_unknown_kind_returns_none_with_warning(self, caplog):
        """kind="state"（commandでもeventでもない） → None を返し、warningログを出す。"""
        body = {"v": 1, "kind": "state", "data": {"type": "identity"}}
        msg = _make_msg(11, "w-h", body)
        with caplog.at_level(logging.WARNING, logger="src.services.ow_service"):
            result = ow_service._parse_ow_event(msg)
        assert result is None
        assert any("kind=" in r.message for r in caplog.records)

    def test_non_dict_body_returns_none(self):
        """body=None → None を返す。"""
        msg = _make_msg(12, "w-h", None)
        result = ow_service._parse_ow_event(msg)
        assert result is None

    def test_old_kind_cmd_returns_none_with_warning(self, caplog):
        """旧形式 kind=cmd → None を返し warning ログを出す（v3 cutoff）"""
        body = {"v": 1, "kind": "cmd", "from": "orch", "to": "w-a", "verb": "assign"}
        msg = _make_msg(20, "orch", body)
        with caplog.at_level(logging.WARNING, logger="src.services.ow_service"):
            result = ow_service._parse_ow_event(msg)
        assert result is None
        assert any("kind=" in r.message for r in caplog.records)


# ----------------------------
# TestQueryLatestEvent
# ----------------------------


class TestQueryLatestEvent:
    """_query_latest_event のユニットテスト。"""

    def test_returns_latest_by_msg_id(self, monkeypatch):
        """同じdata_typeが2件 → msg_id大きい方を返す。"""
        msgs = [
            _make_msg(1, "w-h", _event_body("state", {"state": "loading"}), "2026-06-14T10:00:00+00:00"),
            _make_msg(5, "w-h", _event_body("state", {"state": "working"}), "2026-06-14T10:01:00+00:00"),
        ]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service._query_latest_event("ch", "w-h", "state")
        assert result is not None
        assert result["msg_id"] == 5

    def test_handle_filter(self, monkeypatch):
        """複数handleがいる時、指定handleのみ返す。"""
        msgs = [
            _make_msg(1, "w-a", _event_body("state", {"state": "working"}, handle="w-a")),
            _make_msg(2, "w-b", _event_body("state", {"state": "loading"}, handle="w-b")),
        ]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service._query_latest_event("ch", "w-a", "state")
        assert result is not None
        assert result["handle"] == "w-a"

    def test_handle_none_returns_any_handle(self, monkeypatch):
        """handle=Noneなら全handleを対象にする。"""
        msgs = [
            _make_msg(1, "w-a", _event_body("state", {"state": "working"}, handle="w-a")),
            _make_msg(3, "w-b", _event_body("state", {"state": "loading"}, handle="w-b")),
        ]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service._query_latest_event("ch", None, "state")
        assert result is not None
        assert result["msg_id"] == 3

    def test_command_kind_skipped(self, monkeypatch):
        """kind="command" のメッセージはスキップされる。"""
        body = {"v": 1, "kind": "command", "data": {"type": "state"}}
        msgs = [_make_msg(1, "w-h", body)]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service._query_latest_event("ch", "w-h", "state")
        assert result is None

    def test_history_error_returns_none(self, monkeypatch):
        """ow_historyがerrorを返す → None。"""
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: {"error": "conn refused"})
        result = ow_service._query_latest_event("ch", "w-h", "state")
        assert result is None

    def test_since_parameter_passed_to_history(self, monkeypatch):
        """sinceパラメータがow_historyに渡される。"""
        received = {}

        def fake_history(channel, since=0, limit=100):
            received["since"] = since
            return _make_history([])

        monkeypatch.setattr(ow_service, "ow_history", fake_history)
        ow_service._query_latest_event("ch", "w-h", "state", since=42)
        assert received["since"] == 42


# ----------------------------
# TestQueryEventsSince
# ----------------------------


class TestQueryEventsSince:
    """_query_events_since のユニットテスト。"""

    def test_returns_all_matching(self, monkeypatch):
        """複数件返す。"""
        msgs = [
            _make_msg(1, "w-h", _event_body("heartbeat", {"phase": "working"}), "2026-06-14T10:00:00+00:00"),
            _make_msg(2, "w-h", _event_body("heartbeat", {"phase": "working"}), "2026-06-14T10:01:00+00:00"),
        ]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service._query_events_since("ch", "w-h", "heartbeat")
        assert len(result) == 2

    def test_data_type_none_matches_all_events(self, monkeypatch):
        """data_type=Noneなら全eventが返る。"""
        msgs = [
            _make_msg(1, "w-h", _event_body("state", {"state": "working"})),
            _make_msg(2, "w-h", _event_body("heartbeat", {"phase": "working"})),
        ]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service._query_events_since("ch", "w-h", None)
        assert len(result) == 2

    def test_history_error_returns_empty(self, monkeypatch):
        """ow_historyがerror → 空リスト。"""
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: {"error": "conn refused"})
        result = ow_service._query_events_since("ch", "w-h", "state")
        assert result == []


# ----------------------------
# TestInferCrashCause
# ----------------------------


class TestInferCrashCause:
    """_infer_crash_cause のユニットテスト。"""

    def _old_hb(self, seconds_ago=200):
        """現在時刻からseconds_ago秒前のISO文字列を返す。"""
        dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        return dt.isoformat()

    def _recent_hb(self, seconds_ago=10):
        """直近のheartbeat時刻。"""
        dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        return dt.isoformat()

    def test_non_terminal_state_with_timeout_returns_crashed(self):
        """state="working", 古いheartbeat → "crashed (inferred)"。"""
        result = ow_service._infer_crash_cause("working", self._old_hb(200))
        assert result == "crashed (inferred)"

    def test_draining_with_timeout_returns_crashed_during_drain(self):
        """state="draining", 古いheartbeat → "crashed-during-drain (inferred)"。"""
        result = ow_service._infer_crash_cause("draining", self._old_hb(200))
        assert result == "crashed-during-drain (inferred)"

    def test_terminal_state_returns_none(self):
        """state="terminated" → None。"""
        result = ow_service._infer_crash_cause("terminated", self._old_hb(200))
        assert result is None

    def test_recent_heartbeat_returns_none(self):
        """state="working", 直近heartbeat → None（まだ生きてる）。"""
        result = ow_service._infer_crash_cause("working", self._recent_hb(10))
        assert result is None

    def test_none_heartbeat_returns_none(self):
        """last_heartbeat_at=None → None。"""
        result = ow_service._infer_crash_cause("working", None)
        assert result is None

    def test_escalated_state_returns_none(self):
        """state="escalated"（人間対話中）は heartbeat が古くても crash 推論対象外。"""
        result = ow_service._infer_crash_cause("escalated", self._old_hb(600))
        assert result is None


# ----------------------------
# TestOwGetIdentity
# ----------------------------


class TestOwGetIdentity:
    """ow_get_identity のユニットテスト。"""

    def _setup_history(self, monkeypatch, messages):
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(messages))

    def test_returns_identity_with_msg_id_and_at(self, monkeypatch):
        """正常系: identity eventがある → bundle + msg_id + identity_at を返す。"""
        msgs = [
            _make_msg(
                1, "w-h",
                _event_body("identity", {"alias": "worker-1", "channel": "ch"}),
                "2026-06-14T10:00:00+00:00",
            ),
        ]
        self._setup_history(monkeypatch, msgs)
        result = ow_service.ow_get_identity("ch", "w-h")
        assert result is not None
        assert result["alias"] == "worker-1"
        assert result["msg_id"] == 1
        assert result["identity_at"] == "2026-06-14T10:00:00+00:00"

    def test_returns_none_when_no_identity(self, monkeypatch):
        """identity eventなし → None。"""
        self._setup_history(monkeypatch, [])
        result = ow_service.ow_get_identity("ch", "w-h")
        assert result is None

    def test_inferred_cause_added_on_crash(self, monkeypatch):
        """workingで古いheartbeat → inferred_cause付き。"""
        old_hb = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
        msgs = [
            _make_msg(1, "w-h", _event_body("identity", {"alias": "worker-1"}), "2026-06-14T09:00:00+00:00"),
            _make_msg(2, "w-h", _event_body("state", {"state": "working"}), "2026-06-14T09:01:00+00:00"),
            _make_msg(3, "w-h", _event_body("heartbeat", {"phase": "working"}), old_hb),
        ]
        self._setup_history(monkeypatch, msgs)
        result = ow_service.ow_get_identity("ch", "w-h")
        assert result is not None
        assert result.get("inferred_cause") == "crashed (inferred)"

    @pytest.mark.parametrize(
        "term_ref_value",
        [
            "%5",  # tmux pane_id
            "12345678-1234-1234-1234-123456789ABC",  # iterm2 UUID
            "manual:mac-mini:12345",  # manual fallback
        ],
    )
    def test_term_ref_round_trip(self, monkeypatch, term_ref_value):
        """段階①: event:identity の term_ref が ow_get_identity 戻り値にそのまま乗る。

        reducer は dict(data) で透過保持する。tmux %N / iterm2 UUID / manual いずれの
        形式でも値は加工せず保存される。
        """
        msgs = [
            _make_msg(
                1, "w-h",
                _event_body("identity", {"alias": "worker-1", "term_ref": term_ref_value}),
                "2026-06-14T10:00:00+00:00",
            ),
        ]
        self._setup_history(monkeypatch, msgs)
        result = ow_service.ow_get_identity("ch", "w-h")
        assert result is not None
        assert result.get("term_ref") == term_ref_value
        # validator が呼び出し側で利用できることを担保（reducer 自体は加工しない）。
        assert ow_service.is_valid_term_ref(term_ref_value) is True

    def test_identity_without_term_ref_works(self, monkeypatch):
        """後方互換: term_ref フィールドを持たない identity event でも reducer は正常動作する。

        worker が manual モードや term_ref 取得失敗時に term_ref を省略するケースを想定。
        戻り値には term_ref キーが存在しない（または None）状態となり、他のフィールドは
        通常通り取り出せる。
        """
        msgs = [
            _make_msg(
                1, "w-h",
                _event_body("identity", {"alias": "worker-1", "channel": "ch"}),
                "2026-06-14T10:00:00+00:00",
            ),
        ]
        self._setup_history(monkeypatch, msgs)
        result = ow_service.ow_get_identity("ch", "w-h")
        assert result is not None
        assert result["alias"] == "worker-1"
        # term_ref キーは無いか、あっても None
        assert result.get("term_ref") is None
        # validator は欠落値を invalid と判定する
        assert ow_service.is_valid_term_ref(result.get("term_ref")) is False


# ----------------------------
# TestOwListIdentities
# ----------------------------


class TestOwListIdentities:
    """ow_list_identities のユニットテスト。"""

    def test_returns_all_handles(self, monkeypatch):
        """2つのhandleそれぞれのidentityを返す。"""
        msgs = [
            _make_msg(1, "w-a", _event_body("identity", {"alias": "worker-a"}, handle="w-a")),
            _make_msg(2, "w-b", _event_body("identity", {"alias": "worker-b"}, handle="w-b")),
        ]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service.ow_list_identities("ch")
        aliases = {e["alias"] for e in result}
        assert aliases == {"worker-a", "worker-b"}

    @pytest.mark.parametrize(
        "terminated_payload",
        [
            {"cause": "closed"},
            {"cause": "cancelled"},
            {"cause": "dead"},
            # terminated_at のみ（cause なし）も除外対象
            {"terminated_at": "2026-06-16T00:00:00Z"},
        ],
    )
    def test_alive_only_excludes_terminated(self, monkeypatch, terminated_payload):
        """alive_only=True → cause=closed/cancelled/dead と terminated_at を除外する。"""
        terminated_identity = {"alias": "worker-b", **terminated_payload}
        msgs = [
            _make_msg(1, "w-a", _event_body("identity", {"alias": "worker-a"}, handle="w-a")),
            _make_msg(2, "w-b", _event_body("identity", terminated_identity, handle="w-b")),
        ]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service.ow_list_identities("ch", alive_only=True)
        assert len(result) == 1
        assert result[0]["alias"] == "worker-a"

    def test_term_ref_preserved_per_handle(self, monkeypatch):
        """段階①: 複数 handle の identity に term_ref が個別に乗る。

        ow_list_identities は handle 単位で最新 identity を集約するが、各 entry の
        term_ref は元 event のものをそのまま保持し、混線しない。
        """
        msgs = [
            _make_msg(
                1, "w-a",
                _event_body("identity", {"alias": "worker-a", "term_ref": "%5"}, handle="w-a"),
            ),
            _make_msg(
                2, "w-b",
                _event_body(
                    "identity",
                    {
                        "alias": "worker-b",
                        "term_ref": "12345678-1234-1234-1234-123456789ABC",
                    },
                    handle="w-b",
                ),
            ),
        ]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service.ow_list_identities("ch")
        by_handle = {e["alias"]: e for e in result}
        assert by_handle["worker-a"]["term_ref"] == "%5"
        assert by_handle["worker-b"]["term_ref"] == "12345678-1234-1234-1234-123456789ABC"

    def test_identities_without_term_ref_work(self, monkeypatch):
        """後方互換: term_ref を持たない identity event でも ow_list_identities は正常動作する。

        term_ref 省略はあくまで「フィールドが無い」状態であり、reducer は他のフィールドを
        通常通り集約する。term_ref キーは戻り値に存在しない（None）。
        """
        msgs = [
            _make_msg(1, "w-a", _event_body("identity", {"alias": "worker-a"}, handle="w-a")),
            _make_msg(2, "w-b", _event_body("identity", {"alias": "worker-b"}, handle="w-b")),
        ]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service.ow_list_identities("ch")
        by_handle = {e["alias"]: e for e in result}
        assert set(by_handle.keys()) == {"worker-a", "worker-b"}
        assert by_handle["worker-a"].get("term_ref") is None
        assert by_handle["worker-b"].get("term_ref") is None


# ----------------------------
# TestTermRefValidation
# ----------------------------


class TestTermRefValidation:
    """段階① identity bundle ヘルパー: is_valid_term_ref / classify_term_ref のテスト。"""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("%0", "tmux"),
            ("%5", "tmux"),
            ("%123", "tmux"),
            ("12345678-1234-1234-1234-123456789ABC", "iterm2"),
            ("abcdef01-2345-6789-abcd-ef0123456789", "iterm2"),
            ("manual:mac-mini:12345", "manual"),
            ("manual:host-with-dash:1", "manual"),
        ],
    )
    def test_classify_valid_formats(self, value, expected):
        """tmux %N / iterm2 UUID / manual:host:pid を正しく分類する。"""
        assert ow_service.classify_term_ref(value) == expected
        assert ow_service.is_valid_term_ref(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "abc",  # 未知形式
            "%",  # tmux pattern に %N の数字部が無い
            "%abc",  # tmux pattern は数字のみ
            "12345678-1234-1234-1234",  # UUID truncated
            "12345678-1234-1234-1234-123456789ABCDEF",  # UUID 末尾過多
            "manual:host:abc",  # pid が数字でない
            "manual::123",  # host 部が空
            123,  # 非文字列
        ],
    )
    def test_invalid_formats_return_none_and_false(self, value):
        """未知形式・None・空文字・非文字列は classify=None / is_valid=False。"""
        assert ow_service.classify_term_ref(value) is None
        assert ow_service.is_valid_term_ref(value) is False


# ----------------------------
# TestOwGetPresence
# ----------------------------


class TestOwGetPresence:
    """ow_get_presence のユニットテスト。"""

    def test_online_with_recent_heartbeat(self, monkeypatch):
        """直近heartbeat → online。"""
        recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        msgs = [
            _make_msg(1, "w-h", _event_body("heartbeat", {"phase": "working"}), recent),
        ]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service.ow_get_presence("ch", "w-h")
        assert result["status"] == "online"
        assert result["handle"] == "w-h"

    def test_offline_with_old_heartbeat(self, monkeypatch):
        """古いheartbeat → offline。"""
        old = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
        msgs = [
            _make_msg(1, "w-h", _event_body("heartbeat", {"phase": "working"}), old),
        ]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service.ow_get_presence("ch", "w-h")
        assert result["status"] == "offline"

    def test_unknown_when_no_heartbeat(self, monkeypatch):
        """heartbeatなし → unknown。"""
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history([]))
        result = ow_service.ow_get_presence("ch", "w-h")
        assert result["status"] == "unknown"
        assert result["last_heartbeat_at"] is None


# ----------------------------
# TestOwGetWorkloadState
# ----------------------------


class TestOwGetWorkloadState:
    """ow_get_workload_state のユニットテスト。"""

    def test_returns_latest_state(self, monkeypatch):
        """最新stateを返す。"""
        msgs = [
            _make_msg(1, "w-h", _event_body("state", {"state": "loading"}), "2026-06-14T10:00:00+00:00"),
            _make_msg(2, "w-h", _event_body("state", {"state": "working", "cause": None}), "2026-06-14T10:01:00+00:00"),
        ]
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history(msgs))
        result = ow_service.ow_get_workload_state("ch", "w-h")
        assert result is not None
        assert result["state"] == "working"
        assert result["handle"] == "w-h"
        assert result["msg_id"] == 2
        assert result["state_at"] == "2026-06-14T10:01:00+00:00"

    def test_returns_none_when_no_state(self, monkeypatch):
        """stateなし → None。"""
        monkeypatch.setattr(ow_service, "ow_history", lambda *a, **kw: _make_history([]))
        result = ow_service.ow_get_workload_state("ch", "w-h")
        assert result is None
