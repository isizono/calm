"""scripts/ow/sentinel.py の純粋ロジックを検証する unit test。

SentinelState が以下の通り動作することを確認する:

- ready→working は 60秒、draining→terminated は 90秒で stagnation 発火
- 同一 (handle, state) は 1回だけ通知 (重複抑止)
- state 遷移 (entry 差し替え) で再武装、二度目以降の ready 滞留でも再発火
- identity (terminated_at) で watch 解除
- loading は監視対象外
- 複数 handle は独立して追跡される
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.ow.sentinel import (  # noqa: E402
    SENTINEL_HANDLE,
    SentinelState,
    _coerce_message_body,
)


def _state_msg(handle: str, state: str, task: str | None = None) -> dict:
    body: dict = {
        "v": 1,
        "kind": "event",
        "from": handle,
        "to": "orch",
        "data": {"type": "state", "state": state},
    }
    if task is not None:
        body["task"] = task
    return {"msg_id": 1, "handle": handle, "body": body}


def _identity_msg(handle: str, *, terminated_at: str | None = None) -> dict:
    data: dict = {"type": "identity", "handle": handle}
    if terminated_at is not None:
        data["terminated_at"] = terminated_at
    return {
        "msg_id": 1,
        "handle": handle,
        "body": {
            "v": 1,
            "kind": "event",
            "from": handle,
            "to": "*",
            "data": data,
        },
    }


def test_ready_60sec_triggers_stagnation():
    s = SentinelState()
    s.observe_event(_state_msg("w-a", "ready", task="T1"), now=0.0)

    assert s.scan(59.0) == []

    envelopes = s.scan(60.0)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env["from"] == SENTINEL_HANDLE
    assert env["to"] == "orch"
    assert env["kind"] == "event"
    assert env["v"] == 1
    assert env["task"] == "T1"
    data = env["data"]
    assert data == {
        "type": "stagnation",
        "target_handle": "w-a",
        "target_state": "ready",
        "elapsed_sec": 60,
        "threshold_sec": 60,
    }


def test_draining_90sec_triggers_stagnation():
    s = SentinelState()
    s.observe_event(_state_msg("w-b", "draining"), now=0.0)

    assert s.scan(89.0) == []

    envelopes = s.scan(90.0)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env["data"]["target_state"] == "draining"
    assert env["data"]["threshold_sec"] == 90
    assert env["data"]["target_handle"] == "w-b"
    # task が未指定なら envelope に task キーは含まれない
    assert "task" not in env


def test_duplicate_suppression_for_same_state():
    """同一 (handle, state) で mark_emitted 後は scan を何度呼んでも返らない。"""
    s = SentinelState()
    s.observe_event(_state_msg("w-a", "ready"), now=0.0)

    first = s.scan(60.0)
    assert len(first) == 1
    # 送信成功を mark
    s.mark_emitted("w-a", "ready")

    # さらに時間が経っても再発火しない
    assert s.scan(120.0) == []
    assert s.scan(600.0) == []


def test_scan_returns_envelope_again_when_not_marked_emitted():
    """送信失敗で mark_emitted を呼ばないと次回 scan で再度返る (retry)。"""
    s = SentinelState()
    s.observe_event(_state_msg("w-a", "ready"), now=0.0)

    first = s.scan(60.0)
    assert len(first) == 1
    # mark_emitted を呼ばない (= 送信失敗を模擬)
    second = s.scan(65.0)
    assert len(second) == 1
    assert second[0]["data"]["target_handle"] == "w-a"

    # ようやく成功 → 以降は出ない
    s.mark_emitted("w-a", "ready")
    assert s.scan(70.0) == []


def test_mark_emitted_ignores_state_mismatch_after_rearm():
    """mark_emitted は古い state の確定が新 watch entry に影響しないこと。"""
    s = SentinelState()
    s.observe_event(_state_msg("w-a", "ready"), now=0.0)
    assert len(s.scan(60.0)) == 1
    # ready の mark を立てる前に state 遷移して draining に再武装
    s.observe_event(_state_msg("w-a", "draining"), now=70.0)
    # 古い state の mark は新 entry に影響しない
    s.mark_emitted("w-a", "ready")
    # draining 閾値超過で発火できる
    envelopes = s.scan(170.0)
    assert len(envelopes) == 1
    assert envelopes[0]["data"]["target_state"] == "draining"


def test_mark_emitted_for_unknown_handle_is_noop():
    s = SentinelState()
    s.mark_emitted("nonexistent", "ready")  # 例外を出さない
    assert s.watches == {}


def test_rearm_after_state_transition_back_to_ready():
    """ready→working で watch 解除、再度 ready に戻ったら 60秒で再発火する。"""
    s = SentinelState()
    s.observe_event(_state_msg("w-a", "ready"), now=0.0)
    assert len(s.scan(60.0)) == 1
    s.mark_emitted("w-a", "ready")

    # ready → working: 監視対象外なので watch 解除
    s.observe_event(_state_msg("w-a", "working"), now=70.0)
    assert s.scan(200.0) == []

    # working → ready: 新規 watch entry (emitted=False)
    s.observe_event(_state_msg("w-a", "ready"), now=300.0)
    assert s.scan(359.0) == []

    envelopes = s.scan(360.0)
    assert len(envelopes) == 1
    assert envelopes[0]["data"]["target_state"] == "ready"
    assert envelopes[0]["data"]["target_handle"] == "w-a"


def test_terminated_identity_clears_watch():
    s = SentinelState()
    s.observe_event(_state_msg("w-a", "draining"), now=0.0)
    s.observe_event(_identity_msg("w-a", terminated_at="2026-06-19T17:00:00Z"), now=30.0)

    # 監視解除済みなので閾値を超えても発火しない
    assert s.scan(120.0) == []


def test_identity_without_terminated_does_not_clear_watch():
    """spawn 時の identity (terminated_at 無し) は watch を解除しない。"""
    s = SentinelState()
    s.observe_event(_state_msg("w-a", "ready"), now=0.0)
    s.observe_event(_identity_msg("w-a"), now=10.0)

    envelopes = s.scan(60.0)
    assert len(envelopes) == 1


def test_loading_state_is_not_monitored():
    """loading→ready は監視対象外 (warm-up 中の長時間滞留を許容するため)。"""
    s = SentinelState()
    s.observe_event(_state_msg("w-a", "loading"), now=0.0)

    assert s.scan(120.0) == []
    # 内部状態にも entry が積まれない
    assert "w-a" not in s.watches


def test_blocked_and_escalated_states_are_not_monitored():
    """blocked / escalated はそれぞれ orch 回答待ち・人間対話中なので対象外。"""
    s = SentinelState()
    s.observe_event(_state_msg("w-a", "blocked"), now=0.0)
    s.observe_event(_state_msg("w-b", "escalated"), now=0.0)

    assert s.scan(600.0) == []


def test_multiple_handles_tracked_independently():
    s = SentinelState()
    s.observe_event(_state_msg("w-a", "ready"), now=0.0)
    s.observe_event(_state_msg("w-b", "draining"), now=10.0)

    # t=70: w-a だけ閾値到達 (60秒経過)、w-b はまだ 60秒
    envelopes_70 = s.scan(70.0)
    assert len(envelopes_70) == 1
    assert envelopes_70[0]["data"]["target_handle"] == "w-a"
    s.mark_emitted("w-a", "ready")

    # t=100: w-b も 90秒経過で発火
    envelopes_100 = s.scan(100.0)
    assert len(envelopes_100) == 1
    assert envelopes_100[0]["data"]["target_handle"] == "w-b"
    assert envelopes_100[0]["data"]["target_state"] == "draining"


def test_working_event_after_ready_within_threshold_clears_watch():
    """ready 受信後すぐ working が来た正常ケースは発火しない。"""
    s = SentinelState()
    s.observe_event(_state_msg("w-a", "ready"), now=0.0)
    s.observe_event(_state_msg("w-a", "working"), now=5.0)

    assert s.scan(60.0) == []
    assert s.scan(120.0) == []


def test_observe_event_ignores_non_state_non_identity_types():
    """heartbeat 等は watch に影響しない。"""
    s = SentinelState()
    s.observe_event(_state_msg("w-a", "ready"), now=0.0)
    # heartbeat はスキップ
    s.observe_event(
        {
            "msg_id": 1,
            "handle": "w-a",
            "body": {
                "v": 1,
                "kind": "event",
                "from": "w-a",
                "to": "*",
                "data": {"type": "heartbeat", "phase": "ready"},
            },
        },
        now=30.0,
    )

    envelopes = s.scan(60.0)
    assert len(envelopes) == 1


def test_observe_event_skips_messages_without_dict_body():
    s = SentinelState()
    # body が parse 不能な文字列のケースは無視される
    s.observe_event({"msg_id": 1, "handle": "w-a", "body": "not a dict"}, now=0.0)
    s.observe_event({"msg_id": 2, "handle": "w-a"}, now=0.0)
    assert s.watches == {}


def test_observe_event_coerces_json_string_body_internally():
    """body が JSON 文字列で渡されても observe_event 内部で dict 化される。"""
    s = SentinelState()
    s.observe_event(
        {
            "msg_id": 1,
            "handle": "w-a",
            "body": '{"v":1,"from":"w-a","data":{"type":"state","state":"ready"}}',
        },
        now=0.0,
    )
    assert "w-a" in s.watches


def test_observe_event_uses_handle_fallback_when_from_missing():
    """body.from が無い場合は message.handle で識別する。"""
    s = SentinelState()
    s.observe_event(
        {
            "msg_id": 1,
            "handle": "w-a",
            "body": {
                "v": 1,
                "kind": "event",
                "to": "orch",
                "data": {"type": "state", "state": "ready"},
            },
        },
        now=0.0,
    )
    assert "w-a" in s.watches


def test_custom_thresholds_override_defaults():
    s = SentinelState(thresholds={"ready": 10, "draining": 20})
    s.observe_event(_state_msg("w-a", "ready"), now=0.0)
    s.observe_event(_state_msg("w-b", "draining"), now=0.0)

    envelopes = s.scan(10.0)
    handles = {e["data"]["target_handle"] for e in envelopes}
    assert handles == {"w-a"}
    s.mark_emitted("w-a", "ready")

    envelopes2 = s.scan(20.0)
    handles2 = {e["data"]["target_handle"] for e in envelopes2}
    assert handles2 == {"w-b"}


def test_coerce_message_body_parses_json_string():
    raw = {
        "msg_id": 1,
        "handle": "w-a",
        "body": '{"v":1,"from":"w-a","data":{"type":"state","state":"ready"}}',
    }
    coerced = _coerce_message_body(raw)
    assert isinstance(coerced["body"], dict)
    assert coerced["body"]["data"]["state"] == "ready"
    # 元 dict は破壊しない
    assert isinstance(raw["body"], str)


def test_coerce_message_body_passes_through_dict_body():
    raw = {
        "msg_id": 1,
        "handle": "w-a",
        "body": {"v": 1, "from": "w-a", "data": {"type": "state", "state": "ready"}},
    }
    coerced = _coerce_message_body(raw)
    assert coerced is raw or coerced["body"] is raw["body"]


def test_coerce_message_body_returns_original_on_invalid_json():
    raw = {"msg_id": 1, "handle": "w-a", "body": "not-json"}
    coerced = _coerce_message_body(raw)
    assert coerced["body"] == "not-json"
