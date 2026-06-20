"""ow_service reducer 4関数のユニットテスト。

reducer 系 4関数 (ow_get_identity / ow_list_identities / ow_get_presence /
ow_get_workload_state) と内部ヘルパー (_query_latest_event / _latest_events_by_type)
は OwState キャッシュを読むだけで relay を直接叩かない。本テストは cache fastpath
の振る舞いを検証する。tmp 隔離は ``_isolated_state_dir`` で OW_STATE_DIR を切り替える。
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.services import ow_service
from src.services.ow.cache import CURRENT_SCHEMA_VERSION, save_state


# ----------------------------
# テストヘルパー
# ----------------------------


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """OW_STATE_DIR を tmp に閉じて cache 副作用を分離する。"""
    monkeypatch.setenv("OW_STATE_DIR", str(tmp_path))
    return tmp_path


def _make_msg(msg_id, handle, body, created_at=None):
    """テスト用リレーメッセージを生成する (_parse_ow_event の入力形式)。"""
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


def _entry(msg_id: int, data: dict, created_at: str) -> dict:
    """OwState EventEntry 形式を組み立てる。"""
    return {"msg_id": msg_id, "data": dict(data), "created_at": created_at}


def _save_cache(
    channel: str,
    topic_id: int = 454,
    *,
    identity_events: dict | None = None,
    states: dict | None = None,
    heartbeats: dict | None = None,
    identities: dict | None = None,
    workers: dict | None = None,
    last_msg_id: int = 0,
    updated_at: str = "2026-06-19T17:00:00+00:00",
) -> None:
    """テストで OwState を直接 cache に書き出す。

    reducer が `_load_state_by_channel(channel)` で find_topic_id_by_channel →
    load_state する経路を通すため、channel フィールドを必ず一致させる。
    """
    state = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "channel": channel,
        "last_msg_id": last_msg_id,
        "workers": workers or {},
        "identities": identities or {},
        "identity_events": identity_events or {},
        "states": states or {},
        "heartbeats": heartbeats or {},
        "presence": [],
        "updated_at": updated_at,
    }
    save_state(topic_id, state)


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
# TestQueryLatestEvent (cache fastpath)
# ----------------------------


class TestQueryLatestEvent:
    """_query_latest_event の cache fastpath ユニットテスト。

    本関数は OwState の states / heartbeats / identity_events を読むだけで、
    relay を直接叩かない。cache miss = None。
    """

    def test_returns_event_for_cached_state(self):
        """cache に states[w-h] がある → parsed event 形式で返る。"""
        _save_cache(
            "ch",
            states={
                "w-h": _entry(5, {"type": "state", "state": "working"}, "2026-06-14T10:01:00+00:00"),
            },
        )
        result = ow_service._query_latest_event("ch", "w-h", "state")
        assert result is not None
        assert result["msg_id"] == 5
        assert result["handle"] == "w-h"
        assert result["body"]["kind"] == "event"
        assert result["body"]["data"]["state"] == "working"

    def test_handle_filter(self):
        """指定 handle のみマッチする (他 handle の entry は無視)。"""
        _save_cache(
            "ch",
            states={
                "w-a": _entry(1, {"type": "state", "state": "working"}, "2026-06-14T10:00:00+00:00"),
                "w-b": _entry(2, {"type": "state", "state": "loading"}, "2026-06-14T10:01:00+00:00"),
            },
        )
        result = ow_service._query_latest_event("ch", "w-a", "state")
        assert result is not None
        assert result["handle"] == "w-a"
        assert result["body"]["data"]["state"] == "working"

    def test_handle_none_returns_max_msg_id(self):
        """handle=None → states 内の全 handle から最大 msg_id を選ぶ。"""
        _save_cache(
            "ch",
            states={
                "w-a": _entry(1, {"type": "state", "state": "working"}, "..."),
                "w-b": _entry(3, {"type": "state", "state": "loading"}, "..."),
            },
        )
        result = ow_service._query_latest_event("ch", None, "state")
        assert result is not None
        assert result["msg_id"] == 3
        assert result["handle"] == "w-b"

    def test_cache_miss_returns_none(self):
        """cache が無い (find_topic_id_by_channel が見つけられない) → None。"""
        result = ow_service._query_latest_event("ch", "w-h", "state")
        assert result is None

    def test_cache_for_other_channel_returns_none(self):
        """cache はあるが channel mismatch → load_state が None → None。"""
        _save_cache(
            "another-channel",
            states={"w-h": _entry(5, {"type": "state", "state": "working"}, "...")},
        )
        result = ow_service._query_latest_event("ch", "w-h", "state")
        assert result is None

    def test_since_excludes_entries_with_msg_id_le_since(self):
        """since パラメータ: msg_id <= since の entry は除外する。"""
        _save_cache(
            "ch",
            states={
                "w-h": _entry(5, {"type": "state", "state": "working"}, "..."),
            },
        )
        # since=5 → cached msg_id 5 は除外 → None
        result = ow_service._query_latest_event("ch", "w-h", "state", since=5)
        assert result is None
        # since=4 → cached msg_id 5 が残る → 返る
        result = ow_service._query_latest_event("ch", "w-h", "state", since=4)
        assert result is not None
        assert result["msg_id"] == 5

    def test_unsupported_data_type_returns_none(self):
        """対応していない data_type (例: "unknown") は cache に該当領域なし → None。"""
        _save_cache(
            "ch",
            states={"w-h": _entry(5, {"type": "state", "state": "working"}, "...")},
        )
        result = ow_service._query_latest_event("ch", "w-h", "unknown_type")
        assert result is None


# ----------------------------
# TestQueryEventsSince
# ----------------------------


class TestQueryEventsSince:
    """_query_events_since のユニットテスト。

    本関数は cache fastpath の対象外で、relay full pull を行う。
    """

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
    """ow_get_identity の cache fastpath テスト。"""

    def test_returns_identity_with_msg_id_and_at(self):
        """正常系: cache に identity_events[handle] があれば bundle + msg_id + identity_at を返す。"""
        _save_cache(
            "ch",
            identity_events={
                "w-h": _entry(
                    1,
                    {"type": "identity", "alias": "worker-1", "channel": "ch"},
                    "2026-06-14T10:00:00+00:00",
                ),
            },
        )
        result = ow_service.ow_get_identity("ch", "w-h")
        assert result is not None
        assert result["alias"] == "worker-1"
        assert result["msg_id"] == 1
        assert result["identity_at"] == "2026-06-14T10:00:00+00:00"

    def test_returns_none_when_no_identity(self):
        """cache に identity_events 未登録 → None。"""
        _save_cache("ch")  # empty cache
        result = ow_service.ow_get_identity("ch", "w-h")
        assert result is None

    def test_returns_none_when_cache_missing(self):
        """cache 自体が未生成 → None (relay は叩かない)。"""
        result = ow_service.ow_get_identity("ch", "w-h")
        assert result is None

    def test_inferred_cause_added_on_crash(self):
        """state=working + 古い heartbeat → inferred_cause 付き。"""
        old_hb = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
        _save_cache(
            "ch",
            identity_events={
                "w-h": _entry(1, {"type": "identity", "alias": "worker-1"}, "2026-06-14T09:00:00+00:00"),
            },
            states={
                "w-h": _entry(2, {"type": "state", "state": "working"}, "2026-06-14T09:01:00+00:00"),
            },
            heartbeats={
                "w-h": _entry(3, {"type": "heartbeat", "phase": "working"}, old_hb),
            },
        )
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
    def test_term_ref_round_trip(self, term_ref_value):
        """段階①: event:identity の term_ref が reducer 戻り値にそのまま乗る。"""
        _save_cache(
            "ch",
            identity_events={
                "w-h": _entry(
                    1,
                    {"type": "identity", "alias": "worker-1", "term_ref": term_ref_value},
                    "2026-06-14T10:00:00+00:00",
                ),
            },
        )
        result = ow_service.ow_get_identity("ch", "w-h")
        assert result is not None
        assert result.get("term_ref") == term_ref_value
        assert ow_service.is_valid_term_ref(term_ref_value) is True

    def test_identity_without_term_ref_works(self):
        """後方互換: term_ref キーが無くても他のフィールドは正常取得できる。"""
        _save_cache(
            "ch",
            identity_events={
                "w-h": _entry(
                    1,
                    {"type": "identity", "alias": "worker-1", "channel": "ch"},
                    "2026-06-14T10:00:00+00:00",
                ),
            },
        )
        result = ow_service.ow_get_identity("ch", "w-h")
        assert result is not None
        assert result["alias"] == "worker-1"
        assert result.get("term_ref") is None
        assert ow_service.is_valid_term_ref(result.get("term_ref")) is False


# ----------------------------
# TestOwListIdentities
# ----------------------------


class TestOwListIdentities:
    """ow_list_identities の cache fastpath テスト。"""

    def test_returns_all_handles(self):
        """cache 内の identity_events の全 handle を返す。"""
        _save_cache(
            "ch",
            identity_events={
                "w-a": _entry(1, {"type": "identity", "alias": "worker-a"}, "..."),
                "w-b": _entry(2, {"type": "identity", "alias": "worker-b"}, "..."),
            },
        )
        result = ow_service.ow_list_identities("ch")
        aliases = {e["alias"] for e in result}
        assert aliases == {"worker-a", "worker-b"}

    def test_returns_empty_when_cache_missing(self):
        """cache 未生成 → 空リスト (relay は叩かない)。"""
        result = ow_service.ow_list_identities("ch")
        assert result == []

    @pytest.mark.parametrize(
        "terminated_payload",
        [
            {"cause": "closed"},
            {"cause": "cancelled"},
            {"cause": "dead"},
            {"terminated_at": "2026-06-16T00:00:00Z"},
        ],
    )
    def test_alive_only_excludes_terminated(self, terminated_payload):
        """alive_only=True → cause=closed/cancelled/dead と terminated_at を除外する。"""
        terminated_identity = {
            "type": "identity",
            "alias": "worker-b",
            **terminated_payload,
        }
        _save_cache(
            "ch",
            identity_events={
                "w-a": _entry(1, {"type": "identity", "alias": "worker-a"}, "..."),
                "w-b": _entry(2, terminated_identity, "..."),
            },
        )
        result = ow_service.ow_list_identities("ch", alive_only=True)
        assert len(result) == 1
        assert result[0]["alias"] == "worker-a"

    def test_term_ref_preserved_per_handle(self):
        """段階①: 複数 handle の term_ref が個別に保持される。"""
        _save_cache(
            "ch",
            identity_events={
                "w-a": _entry(
                    1,
                    {"type": "identity", "alias": "worker-a", "term_ref": "%5"},
                    "...",
                ),
                "w-b": _entry(
                    2,
                    {
                        "type": "identity",
                        "alias": "worker-b",
                        "term_ref": "12345678-1234-1234-1234-123456789ABC",
                    },
                    "...",
                ),
            },
        )
        result = ow_service.ow_list_identities("ch")
        by_handle = {e["alias"]: e for e in result}
        assert by_handle["worker-a"]["term_ref"] == "%5"
        assert by_handle["worker-b"]["term_ref"] == "12345678-1234-1234-1234-123456789ABC"

    def test_identities_without_term_ref_work(self):
        """後方互換: term_ref を持たない identity でも他フィールドは集約される。"""
        _save_cache(
            "ch",
            identity_events={
                "w-a": _entry(1, {"type": "identity", "alias": "worker-a"}, "..."),
                "w-b": _entry(2, {"type": "identity", "alias": "worker-b"}, "..."),
            },
        )
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
    """ow_get_presence の cache fastpath テスト。"""

    def test_online_with_recent_heartbeat(self):
        """直近 heartbeat → online。"""
        recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        _save_cache(
            "ch",
            heartbeats={
                "w-h": _entry(1, {"type": "heartbeat", "phase": "working"}, recent),
            },
        )
        result = ow_service.ow_get_presence("ch", "w-h")
        assert result["status"] == "online"
        assert result["handle"] == "w-h"

    def test_offline_with_old_heartbeat(self):
        """古い heartbeat → offline。"""
        old = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
        _save_cache(
            "ch",
            heartbeats={
                "w-h": _entry(1, {"type": "heartbeat", "phase": "working"}, old),
            },
        )
        result = ow_service.ow_get_presence("ch", "w-h")
        assert result["status"] == "offline"

    def test_unknown_when_no_heartbeat_in_cache(self):
        """cache に heartbeats[handle] 無し → unknown / last_heartbeat_at=None。"""
        _save_cache("ch")
        result = ow_service.ow_get_presence("ch", "w-h")
        assert result["status"] == "unknown"
        assert result["last_heartbeat_at"] is None

    def test_unknown_when_cache_missing(self):
        """cache 自体が無い → unknown (relay は叩かない)。"""
        result = ow_service.ow_get_presence("ch", "w-h")
        assert result["status"] == "unknown"
        assert result["last_heartbeat_at"] is None


# ----------------------------
# TestOwGetWorkloadState
# ----------------------------


class TestOwGetWorkloadState:
    """ow_get_workload_state の cache fastpath テスト。"""

    def test_returns_latest_state(self):
        """cache の states[handle] を parsed event 形式で再構築し API 戻り値に変換する。"""
        _save_cache(
            "ch",
            states={
                "w-h": _entry(
                    2,
                    {"type": "state", "state": "working", "cause": None},
                    "2026-06-14T10:01:00+00:00",
                ),
            },
        )
        result = ow_service.ow_get_workload_state("ch", "w-h")
        assert result is not None
        assert result["state"] == "working"
        assert result["handle"] == "w-h"
        assert result["msg_id"] == 2
        assert result["state_at"] == "2026-06-14T10:01:00+00:00"

    def test_returns_none_when_no_state_in_cache(self):
        """cache に states[handle] 無し → None。"""
        _save_cache("ch")
        result = ow_service.ow_get_workload_state("ch", "w-h")
        assert result is None

    def test_returns_none_when_cache_missing(self):
        """cache 自体が無い → None。"""
        result = ow_service.ow_get_workload_state("ch", "w-h")
        assert result is None
