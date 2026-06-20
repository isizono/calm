"""ow_service の projector 経路 (A#911 SP-2 PR-α) のユニットテスト。

project_state_to_cache / get_or_rebuild_state の挙動を、
ow_history を monkeypatch したフィクスチャ relay 履歴に対して検証する。
cache miss / corruption / schema mismatch / channel mismatch の4条件で
load_state が None を返し、projector が再構築する自動 fallback も含む。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.services import ow_service
from src.services.ow.cache import CURRENT_SCHEMA_VERSION


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """各テストで OW_STATE_DIR を tmp_path に向ける (ホーム汚染防止)。"""
    monkeypatch.setenv("OW_STATE_DIR", str(tmp_path))
    return tmp_path


def _iso_offset_seconds(seconds_ago: float) -> str:
    """now - seconds_ago (秒) の UTC ISO8601 を返す。heartbeat の経過時間制御用。"""
    from datetime import timedelta

    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    ).isoformat()


def _make_msg(
    msg_id: int,
    handle: str,
    body: dict,
    created_at: str | None = None,
) -> dict:
    return {
        "msg_id": msg_id,
        "handle": handle,
        "body": body,
        "created_at": created_at or "2026-06-19T10:00:00+00:00",
    }


def _state_body(handle: str, task: str, state_val: str) -> dict:
    return {
        "v": 1,
        "kind": "event",
        "from": handle,
        "to": "orch",
        "task": task,
        "data": {"type": "state", "state": state_val},
    }


def _identity_body(handle: str, alias: str, activity_id: int) -> dict:
    return {
        "v": 1,
        "kind": "event",
        "from": handle,
        "to": "*",
        "data": {
            "type": "identity",
            "role": "worker",
            "handle": handle,
            "alias": alias,
            "activity_id": activity_id,
        },
    }


def _heartbeat_body(handle: str, phase: str = "ready") -> dict:
    return {
        "v": 1,
        "kind": "event",
        "from": handle,
        "to": "*",
        "data": {"type": "heartbeat", "phase": phase},
    }


def _patch_history(monkeypatch: pytest.MonkeyPatch, messages: list[dict]) -> None:
    """ow_history を固定フィクスチャに差し替える。"""

    def _fake_history(channel: str, since: int = 0, limit: int = 100) -> dict:
        return {"messages": list(messages)}

    monkeypatch.setattr(ow_service, "ow_history", _fake_history)


class TestProjectStateToCache:
    """project_state_to_cache 単体: relay events → OwState 構築 → save_state。"""

    def test_relay_events_produce_cache_json_with_workers_identities_presence(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_state_dir: Path
    ) -> None:
        """state/identity/heartbeat の3種 event から OwState を組み立てて cache JSON を書き出す。"""
        recent = _iso_offset_seconds(5.0)  # 5秒前 → online (timeout 90s)
        messages = [
            _make_msg(10, "w-a", _identity_body("w-a", "w-a", 911), recent),
            _make_msg(11, "w-a", _state_body("w-a", "T97", "ready"), recent),
            _make_msg(12, "w-a", _heartbeat_body("w-a", "ready"), recent),
        ]
        _patch_history(monkeypatch, messages)

        result = ow_service.project_state_to_cache(
            topic_id=454, channel="P_test"
        )

        assert result is not None
        assert result["schema_version"] == CURRENT_SCHEMA_VERSION
        assert result["channel"] == "P_test"
        assert result["last_msg_id"] == 12
        assert "w-a" in result["workers"]
        assert result["workers"]["w-a"]["state"] == "ready"
        assert result["workers"]["w-a"]["task"] == "T97"
        assert "w-a" in result["identities"]
        assert result["identities"]["w-a"]["alias"] == "w-a"
        assert "w-a" in result["presence"]

        cache_path = _isolated_state_dir / "topic-454.json"
        assert cache_path.exists()
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        assert data["channel"] == "P_test"
        assert data["last_msg_id"] == 12

    def test_round_trip_via_load_state(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_state_dir: Path
    ) -> None:
        """projector で書いた cache を load_state で読み戻すと OwState が一致する。"""
        from src.services.ow.cache import load_state

        recent = _iso_offset_seconds(2.0)
        messages = [
            _make_msg(50, "w-b", _identity_body("w-b", "w-b", 912), recent),
            _make_msg(51, "w-b", _state_body("w-b", "T98", "working"), recent),
            _make_msg(52, "w-b", _heartbeat_body("w-b", "ready"), recent),
        ]
        _patch_history(monkeypatch, messages)

        projected = ow_service.project_state_to_cache(
            topic_id=454, channel="P_rt"
        )
        loaded = load_state(topic_id=454, channel="P_rt")

        assert projected is not None
        assert loaded is not None
        assert loaded["channel"] == projected["channel"]
        assert loaded["last_msg_id"] == projected["last_msg_id"]
        assert loaded["workers"] == projected["workers"]
        assert loaded["identities"] == projected["identities"]
        assert loaded["presence"] == projected["presence"]

    def test_latest_state_wins_when_multiple_state_events_for_same_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同 handle の state event が複数あるとき、最大 msg_id の state を採用する。"""
        recent = _iso_offset_seconds(1.0)
        messages = [
            _make_msg(100, "w-c", _state_body("w-c", "T1", "loading"), recent),
            _make_msg(101, "w-c", _state_body("w-c", "T1", "ready"), recent),
            _make_msg(102, "w-c", _state_body("w-c", "T1", "working"), recent),
        ]
        _patch_history(monkeypatch, messages)

        result = ow_service.project_state_to_cache(
            topic_id=454, channel="P_x"
        )

        assert result is not None
        assert result["workers"]["w-c"]["state"] == "working"
        assert result["workers"]["w-c"]["latest_msg_id"] == 102
        # 全 state event の最大 msg_id は last_msg_id (state 全体) に反映される
        assert result["last_msg_id"] == 102

    def test_heartbeat_older_than_timeout_excluded_from_presence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """heartbeat が timeout 超過 (>90s) の handle は presence に含めない。"""
        recent = _iso_offset_seconds(2.0)
        stale = _iso_offset_seconds(200.0)  # 200秒前 (>= 90s timeout)
        messages = [
            _make_msg(200, "w-fresh", _identity_body("w-fresh", "w-fresh", 1), recent),
            _make_msg(201, "w-fresh", _heartbeat_body("w-fresh", "ready"), recent),
            _make_msg(202, "w-stale", _identity_body("w-stale", "w-stale", 2), stale),
            _make_msg(203, "w-stale", _heartbeat_body("w-stale", "ready"), stale),
        ]
        _patch_history(monkeypatch, messages)

        result = ow_service.project_state_to_cache(
            topic_id=454, channel="P_pr"
        )

        assert result is not None
        assert "w-fresh" in result["presence"]
        assert "w-stale" not in result["presence"]
        # identity は presence と独立に維持される
        assert "w-stale" in result["identities"]

    def test_returns_none_when_relay_history_errors(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_state_dir: Path
    ) -> None:
        """ow_history が error を返すと projector は None を返し cache ファイルも作らない。"""

        def _fake_history(channel: str, since: int = 0, limit: int = 100) -> dict:
            return {"error": {"code": "RELAY_DOWN", "message": "unreachable"}}

        monkeypatch.setattr(ow_service, "ow_history", _fake_history)

        result = ow_service.project_state_to_cache(
            topic_id=454, channel="P_err"
        )

        assert result is None
        assert not (_isolated_state_dir / "topic-454.json").exists()

    def test_ignores_non_event_kind_and_missing_from(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """kind != event や from 欠落 envelope は集計対象外。"""
        recent = _iso_offset_seconds(1.0)
        command_body = {
            "v": 1,
            "kind": "command",
            "from": "orch",
            "to": "w-z",
            "task": "T1",
            "data": {"type": "assign"},
        }
        no_from_body = {
            "v": 1,
            "kind": "event",
            "from": "",
            "data": {"type": "state", "state": "ready"},
        }
        messages = [
            _make_msg(300, "orch", command_body, recent),
            _make_msg(301, "", no_from_body, recent),
            _make_msg(302, "w-z", _state_body("w-z", "T1", "ready"), recent),
        ]
        _patch_history(monkeypatch, messages)

        result = ow_service.project_state_to_cache(
            topic_id=454, channel="P_ne"
        )

        assert result is not None
        assert list(result["workers"].keys()) == ["w-z"]
        assert result["last_msg_id"] == 302


class TestGetOrRebuildState:
    """get_or_rebuild_state: cache hit 経路と4種類の自動 fallback。"""

    def test_cache_hit_skips_projector(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_state_dir: Path
    ) -> None:
        """cache が有効なら load_state を返し projector を呼ばない。"""
        from src.services.ow.cache import save_state

        save_state(
            topic_id=454,
            state={
                "schema_version": CURRENT_SCHEMA_VERSION,
                "channel": "P_hit",
                "last_msg_id": 555,
                "workers": {"w-x": {"state": "ready", "task": "T1"}},
                "identities": {},
                "presence": [],
                "updated_at": "2026-06-19T10:00:00+00:00",
            },
        )

        called = {"count": 0}

        def _fake_project(topic_id: int, channel: str):
            called["count"] += 1
            return None

        monkeypatch.setattr(ow_service, "project_state_to_cache", _fake_project)

        result = ow_service.get_or_rebuild_state(454, "P_hit")

        assert result is not None
        assert result["channel"] == "P_hit"
        assert result["last_msg_id"] == 555
        assert called["count"] == 0

    def test_fallback_when_cache_missing(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_state_dir: Path
    ) -> None:
        """cache ファイル不存在 → projector が呼ばれて再構築する。"""
        recent = _iso_offset_seconds(1.0)
        messages = [
            _make_msg(10, "w-a", _identity_body("w-a", "w-a", 1), recent),
            _make_msg(11, "w-a", _state_body("w-a", "T1", "ready"), recent),
            _make_msg(12, "w-a", _heartbeat_body("w-a"), recent),
        ]
        _patch_history(monkeypatch, messages)

        assert not (_isolated_state_dir / "topic-454.json").exists()

        result = ow_service.get_or_rebuild_state(454, "P_new")

        assert result is not None
        assert result["channel"] == "P_new"
        assert (_isolated_state_dir / "topic-454.json").exists()

    def test_fallback_when_cache_corrupt(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_state_dir: Path
    ) -> None:
        """壊れた JSON → load_state がファイル削除 → projector で再構築する。"""
        corrupt_path = _isolated_state_dir / "topic-454.json"
        corrupt_path.write_text("not valid json {", encoding="utf-8")

        recent = _iso_offset_seconds(1.0)
        messages = [
            _make_msg(20, "w-b", _state_body("w-b", "T2", "working"), recent),
        ]
        _patch_history(monkeypatch, messages)

        result = ow_service.get_or_rebuild_state(454, "P_corr")

        assert result is not None
        assert result["channel"] == "P_corr"
        # 再生成された cache JSON は有効な構造を持つ
        data = json.loads(corrupt_path.read_text(encoding="utf-8"))
        assert data["schema_version"] == CURRENT_SCHEMA_VERSION
        assert data["channel"] == "P_corr"

    def test_fallback_when_schema_version_mismatches(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_state_dir: Path
    ) -> None:
        """schema_version mismatch → load_state がファイル削除 → projector で再構築する。"""
        path = _isolated_state_dir / "topic-454.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": CURRENT_SCHEMA_VERSION + 999,
                    "channel": "P_sv",
                    "last_msg_id": 1,
                    "workers": {},
                    "identities": {},
                    "presence": [],
                    "updated_at": "2026-06-19T10:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

        recent = _iso_offset_seconds(1.0)
        messages = [
            _make_msg(30, "w-c", _state_body("w-c", "T3", "ready"), recent),
        ]
        _patch_history(monkeypatch, messages)

        result = ow_service.get_or_rebuild_state(454, "P_sv")

        assert result is not None
        assert result["schema_version"] == CURRENT_SCHEMA_VERSION
        assert result["channel"] == "P_sv"

    def test_fallback_when_channel_mismatches(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_state_dir: Path
    ) -> None:
        """channel mismatch → load_state がファイル削除 → projector で再構築する。"""
        from src.services.ow.cache import save_state

        save_state(
            topic_id=454,
            state={
                "schema_version": CURRENT_SCHEMA_VERSION,
                "channel": "P_old",
                "last_msg_id": 1,
                "workers": {},
                "identities": {},
                "presence": [],
                "updated_at": "2026-06-19T10:00:00+00:00",
            },
        )

        recent = _iso_offset_seconds(1.0)
        messages = [
            _make_msg(40, "w-d", _state_body("w-d", "T4", "done"), recent),
        ]
        _patch_history(monkeypatch, messages)

        result = ow_service.get_or_rebuild_state(454, "P_new")

        assert result is not None
        assert result["channel"] == "P_new"
        # 新 channel の最新 state が反映されている
        assert "w-d" in result["workers"]
        assert result["workers"]["w-d"]["state"] == "done"

    def test_fallback_returns_none_when_projector_fails(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_state_dir: Path
    ) -> None:
        """cache miss + relay error → get_or_rebuild_state も None を返す。"""

        def _fake_history(channel: str, since: int = 0, limit: int = 100) -> dict:
            return {"error": {"code": "X", "message": "x"}}

        monkeypatch.setattr(ow_service, "ow_history", _fake_history)

        result = ow_service.get_or_rebuild_state(454, "P_fail")

        assert result is None
        assert not (_isolated_state_dir / "topic-454.json").exists()
