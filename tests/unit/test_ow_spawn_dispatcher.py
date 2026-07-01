"""ow_service: dispatcher session 起動・終了 tool のユニットテスト。

検証範囲:
- `_validate_dispatcher_handle`: 単独での OK/NG 判定 (最小長 4 + d- prefix + kebab-case)
- `ow_spawn_dispatcher`:
  - handle = d-{channel} を自動付与
  - 既存 dispatcher が channel にいたら cascade kill してから新規 spawn
  - ow_spawn_worker を _alias_validator=_validate_dispatcher_handle で呼ぶ
  - channel 不正 (空 / 非文字列) は INVALID_CHANNEL
  - 結果として生成される handle が dispatcher validator で reject される場合は INVALID_HANDLE
  - ow_spawn_worker 失敗 response はそのまま透過
- `ow_close_dispatcher`:
  - cache に channel 状態が無い場合は DISPATCHER_NOT_FOUND
  - dispatcher handle が cache の workers / identities に無い場合は DISPATCHER_NOT_FOUND
  - worker pool (handle prefix w-*) を cascade kill した上で dispatcher 本体を kill
  - dispatcher identity が無い場合は DISPATCHER_IDENTITY_MISSING
  - dispatcher term_ref が無い場合は DISPATCHER_NO_TERM_REF
  - ow_close_worker が dispatcher kill で失敗した場合は DISPATCHER_CLOSE_FAILED
"""
from __future__ import annotations

import logging

import pytest

from src.services import ow_service


# ----------------------------
# _validate_dispatcher_handle
# ----------------------------


class TestValidateDispatcherHandle:
    """dispatcher handle 書式の単体テスト。"""

    @pytest.mark.parametrize(
        "handle",
        [
            "d-a",          # 3 文字 → 最小長 4 未満で NG だが、別パラメータで検証
            "d-",           # prefix のみ
        ],
    )
    def test_too_short_rejected(self, handle: str):
        err = ow_service._validate_dispatcher_handle(handle)
        assert err is not None

    @pytest.mark.parametrize(
        "handle",
        ["d-ab", "d-ow", "d-x1", "d-ow-w1", "d-ow-w3a", "d-very-long-channel-name"],
    )
    def test_valid_handles_pass(self, handle: str):
        assert ow_service._validate_dispatcher_handle(handle) is None

    @pytest.mark.parametrize(
        "handle",
        ["", None, "ow-w1", "w-foo", "o-main", "X-foo", "dispatcher-foo", "d_ow"],
    )
    def test_missing_prefix_rejected(self, handle):
        err = ow_service._validate_dispatcher_handle(handle)
        assert err is not None

    @pytest.mark.parametrize(
        "handle",
        [
            "d-W1",         # 大文字混入
            "d-ow_w1",      # アンダースコア
            "d-ow--w1",     # 連続ハイフン
            "d-ow-",        # 末尾ハイフン
            "d--foo",       # prefix 直後にハイフン (連続扱い)
            "d-プレイ",     # 非 ASCII
            "d-ow w1",      # スペース
        ],
    )
    def test_invalid_chars_after_prefix_rejected(self, handle: str):
        err = ow_service._validate_dispatcher_handle(handle)
        assert err is not None
        assert "invalid characters" in err
        assert "lowercase letter" in err


# ----------------------------
# ow_spawn_dispatcher
# ----------------------------


@pytest.fixture
def spawn_calls(monkeypatch):
    """ow_spawn_worker の呼び出しを記録するフィクスチャ。"""
    calls = []

    def _fake_spawn_worker(**kwargs):
        calls.append(kwargs)
        return {
            "term_ref": "%99",
            "bundle_msg_id": 1,
            "spawning": "ok",
            "alias": kwargs.get("alias", "?"),
        }

    monkeypatch.setattr(ow_service, "ow_spawn_worker", _fake_spawn_worker)
    return calls


class TestOwSpawnDispatcher:
    def test_invalid_channel_empty(self):
        result = ow_service.ow_spawn_dispatcher(channel="", cwd="/tmp", model="opus")
        assert result.get("error", {}).get("code") == "INVALID_CHANNEL"

    def test_invalid_channel_non_string(self):
        result = ow_service.ow_spawn_dispatcher(channel=None, cwd="/tmp", model="opus")  # type: ignore[arg-type]
        assert result.get("error", {}).get("code") == "INVALID_CHANNEL"

    def test_invalid_channel_makes_invalid_handle(self, monkeypatch):
        """channel 名に非 ASCII 文字 → dispatcher handle 検証で reject"""
        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda _ch: None)
        result = ow_service.ow_spawn_dispatcher(channel="プレイ", cwd="/tmp", model="opus")
        assert result.get("error", {}).get("code") == "INVALID_HANDLE"

    def test_basic_spawn_no_existing(self, monkeypatch, spawn_calls):
        """channel に既存 dispatcher 無し → 単純に ow_spawn_worker を呼ぶ"""
        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda _ch: None)
        result = ow_service.ow_spawn_dispatcher(
            channel="ow-w1", cwd="/tmp", model="claude-opus-4-7"
        )
        assert result.get("spawning") == "ok"
        assert len(spawn_calls) == 1
        call = spawn_calls[0]
        assert call["alias"] == "d-ow-w1"
        assert call["channel"] == "ow-w1"
        assert call["_alias_validator"] is ow_service._validate_dispatcher_handle
        assert call["task_n"] == 0

    def test_existing_dispatcher_triggers_cascade_kill(self, monkeypatch, spawn_calls):
        """channel に既存 dispatcher → ow_close_dispatcher が呼ばれてから spawn"""
        close_calls = []

        def _fake_close_dispatcher(channel: str):
            close_calls.append(channel)
            return {"closed": True, "channel": channel, "dispatcher_handle": f"d-{channel}"}

        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda _ch: 42)
        monkeypatch.setattr(
            ow_service, "load_state", lambda _tid, _ch: {"workers": {"d-ow-w1": {"state": "ready"}}}
        )
        monkeypatch.setattr(ow_service, "ow_close_dispatcher", _fake_close_dispatcher)

        result = ow_service.ow_spawn_dispatcher(
            channel="ow-w1", cwd="/tmp", model="claude-opus-4-7"
        )
        assert result.get("spawning") == "ok"
        assert close_calls == ["ow-w1"]
        assert len(spawn_calls) == 1

    def test_cascade_kill_failure_does_not_block_spawn(self, monkeypatch, spawn_calls, caplog):
        """cascade kill が失敗しても新規 spawn は試みる (頭悪く、resurrect しない)"""
        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda _ch: 42)
        monkeypatch.setattr(
            ow_service,
            "load_state",
            lambda _tid, _ch: {"identities": {"d-ow-w1": {"raw": "stuff"}}},
        )
        monkeypatch.setattr(
            ow_service,
            "ow_close_dispatcher",
            lambda _ch: {"error": {"code": "DISPATCHER_CLOSE_FAILED", "message": "oops"}},
        )

        with caplog.at_level(logging.WARNING, logger=ow_service.logger.name):
            result = ow_service.ow_spawn_dispatcher(
                channel="ow-w1", cwd="/tmp", model="claude-opus-4-7"
            )
        assert result.get("spawning") == "ok"
        assert len(spawn_calls) == 1
        assert "cascade kill of existing dispatcher failed" in caplog.text

    def test_spawn_worker_error_passes_through(self, monkeypatch):
        """ow_spawn_worker が error を返したらそのまま透過"""
        def _fake_spawn_worker(**kwargs):
            return {
                "error": {"code": "SPAWN_PRECONDITION_FAILED", "warnings": ["cwd missing"]}
            }

        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda _ch: None)
        monkeypatch.setattr(ow_service, "ow_spawn_worker", _fake_spawn_worker)
        result = ow_service.ow_spawn_dispatcher(
            channel="ow-w1", cwd="/nonexistent", model="claude-opus-4-7"
        )
        assert result.get("error", {}).get("code") == "SPAWN_PRECONDITION_FAILED"


# ----------------------------
# ow_close_dispatcher
# ----------------------------


class TestOwCloseDispatcher:
    def test_invalid_channel_empty(self):
        result = ow_service.ow_close_dispatcher(channel="")
        assert result.get("error", {}).get("code") == "INVALID_CHANNEL"

    def test_no_cached_state_returns_not_found(self, monkeypatch):
        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda _ch: None)
        monkeypatch.setattr(ow_service, "load_state", lambda *_args, **_kw: None)
        result = ow_service.ow_close_dispatcher(channel="ow-w1")
        assert result.get("error", {}).get("code") == "DISPATCHER_NOT_FOUND"

    def test_dispatcher_handle_absent_returns_not_found(self, monkeypatch):
        """cache はあるが d-{channel} が workers/identities に無い → NOT_FOUND"""
        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda _ch: 42)
        monkeypatch.setattr(
            ow_service,
            "load_state",
            lambda *_args, **_kw: {"workers": {"w-a": {}}, "identities": {}},
        )
        result = ow_service.ow_close_dispatcher(channel="ow-w1")
        assert result.get("error", {}).get("code") == "DISPATCHER_NOT_FOUND"

    def test_full_cascade_close_success(self, monkeypatch):
        """worker 2 個 + dispatcher 1 個を cascade kill"""
        close_calls = []

        def _fake_close_worker(term_ref: str):
            close_calls.append(term_ref)
            return {"closed": True, "term_ref": term_ref}

        identities = {
            "w-a": {"term_ref": "%10"},
            "w-b": {"term_ref": "%11"},
            "d-ow-w1": {"term_ref": "%99"},
        }

        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda _ch: 42)
        monkeypatch.setattr(
            ow_service,
            "load_state",
            lambda *_args, **_kw: {
                "workers": {"w-a": {}, "w-b": {}, "d-ow-w1": {}},
                "identities": dict(identities),
            },
        )
        monkeypatch.setattr(
            ow_service, "ow_get_identity", lambda _ch, handle: identities.get(handle)
        )
        monkeypatch.setattr(ow_service, "ow_close_worker", _fake_close_worker)

        result = ow_service.ow_close_dispatcher(channel="ow-w1")
        assert result.get("closed") is True
        assert result.get("channel") == "ow-w1"
        assert result.get("dispatcher_handle") == "d-ow-w1"
        assert sorted(result.get("killed_workers", [])) == ["w-a", "w-b"]
        assert result.get("failed_workers") == []
        # close 順: worker 2 個が先、dispatcher 本体が最後
        assert close_calls[-1] == "%99"
        assert set(close_calls) == {"%10", "%11", "%99"}

    def test_dispatcher_identity_missing(self, monkeypatch):
        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda _ch: 42)
        monkeypatch.setattr(
            ow_service,
            "load_state",
            lambda *_args, **_kw: {"workers": {"d-ow-w1": {}}, "identities": {}},
        )
        monkeypatch.setattr(ow_service, "ow_get_identity", lambda _ch, _h: None)
        result = ow_service.ow_close_dispatcher(channel="ow-w1")
        assert result.get("error", {}).get("code") == "DISPATCHER_IDENTITY_MISSING"

    def test_dispatcher_no_term_ref(self, monkeypatch):
        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda _ch: 42)
        monkeypatch.setattr(
            ow_service,
            "load_state",
            lambda *_args, **_kw: {"workers": {"d-ow-w1": {}}, "identities": {}},
        )
        monkeypatch.setattr(
            ow_service, "ow_get_identity", lambda _ch, _h: {"some_field": "x"}
        )
        result = ow_service.ow_close_dispatcher(channel="ow-w1")
        assert result.get("error", {}).get("code") == "DISPATCHER_NO_TERM_REF"

    def test_dispatcher_close_failed(self, monkeypatch):
        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda _ch: 42)
        monkeypatch.setattr(
            ow_service,
            "load_state",
            lambda *_args, **_kw: {"workers": {"d-ow-w1": {}}, "identities": {}},
        )
        monkeypatch.setattr(
            ow_service, "ow_get_identity", lambda _ch, _h: {"term_ref": "%99"}
        )
        monkeypatch.setattr(
            ow_service,
            "ow_close_worker",
            lambda _t: {"closed": False, "error": {"code": "ADAPTER_CLOSE_TIMEOUT"}},
        )
        result = ow_service.ow_close_dispatcher(channel="ow-w1")
        assert result.get("error", {}).get("code") == "DISPATCHER_CLOSE_FAILED"

    def test_worker_close_failures_recorded(self, monkeypatch):
        """一部の worker close が失敗しても dispatcher 本体は試行、failed_workers に記録"""
        identities = {
            "w-bad": {},  # term_ref なし
            "w-ok": {"term_ref": "%10"},
            "d-ow-w1": {"term_ref": "%99"},
        }
        close_results = {
            "%10": {"closed": True, "term_ref": "%10"},
            "%99": {"closed": True, "term_ref": "%99"},
        }

        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda _ch: 42)
        monkeypatch.setattr(
            ow_service,
            "load_state",
            lambda *_args, **_kw: {
                "workers": {"w-bad": {}, "w-ok": {}, "d-ow-w1": {}},
                "identities": dict(identities),
            },
        )
        monkeypatch.setattr(
            ow_service, "ow_get_identity", lambda _ch, handle: identities.get(handle)
        )
        monkeypatch.setattr(
            ow_service, "ow_close_worker", lambda t: close_results.get(t, {"closed": False})
        )

        result = ow_service.ow_close_dispatcher(channel="ow-w1")
        assert result.get("closed") is True
        assert result.get("killed_workers") == ["w-ok"]
        failed = result.get("failed_workers", [])
        assert any(f.get("handle") == "w-bad" for f in failed)
