"""ow_spawn_worker: spawn-bundle envelope 送信処理と worker_cmd 組み立てのテスト。

D#2952 / D#2953 / D#2962 で確定した cut-over 仕様を検証する:
- event:spawn-bundle envelope が relay に送信されること (to=alias 直送)
- worker_cmd に env (OW_CHANNEL / OW_ALIAS / OW_TASK_N) が注入されること
- worker_cmd の prompt が `/goal ...` 形式であること
- worker_cmd に `--add-dir` が含まれないこと
- spawn-bundle 送信失敗時に SPAWN_PRECONDITION_FAILED で abort すること
"""
import json
from pathlib import Path

import pytest

from src.services import ow_service


@pytest.fixture
def _stub_spawn_environment(monkeypatch):
    """ow_spawn_worker の依存をすべて mock し、relay 送信 body を捕捉する。

    yield された list に ow_send で送信された body dict が順次 append される。
    """
    sent_bodies: list[dict] = []

    def _capture_ow_send(channel, handle, body, needs_reply=False, in_reply_to=None):
        sent_bodies.append(body)
        return {"msg_id": len(sent_bodies)}

    monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
    monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: True)
    monkeypatch.setattr(ow_service, "_get_presence", lambda ch: [])
    monkeypatch.setattr(ow_service, "ow_get_identity", lambda ch, h: None)
    monkeypatch.setattr(ow_service, "_ensure_worker_askuser_deny", lambda c: None)
    monkeypatch.setattr(ow_service, "ow_send", _capture_ow_send)
    # adapter 経路を抑制するため manual fallback に倒す
    monkeypatch.setenv("OW_TERMINAL", "manual")

    return sent_bodies


class TestSpawnBundleEnvelope:
    def test_spawn_sends_spawn_bundle_after_spawning(
        self, _stub_spawn_environment, tmp_path: Path
    ):
        """ow_spawn_worker は spawning broadcast に続いて event:spawn-bundle を送る"""
        result = ow_service.ow_spawn_worker(
            alias="w-bundle01",
            channel="ChAbCdEf",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="バンドル送信テスト",
            acceptance="acceptance本文",
            context="context本文",
            playbook="playbook本文",
            task_n=42,
            activity_id=1234,
            topic_id=567,
        )
        # manual fallback でも spawn-bundle までは送られる
        assert "bundle_msg_id" in result
        assert "command" in result  # manual fallback の command 返却

        # 1 件目: spawning broadcast
        spawning = _stub_spawn_environment[0]
        assert spawning["data"]["type"] == "state"
        assert spawning["data"]["state"] == "spawning"
        assert spawning["to"] == "*"
        # 2 件目: spawn-bundle envelope
        bundle = _stub_spawn_environment[1]
        assert bundle["kind"] == "event"
        assert bundle["to"] == "w-bundle01"  # 直送
        assert bundle["task"] == "T42"
        assert bundle["data"]["type"] == "spawn-bundle"
        assert bundle["data"]["task_title"] == "バンドル送信テスト"
        assert bundle["data"]["acceptance"] == "acceptance本文"
        assert bundle["data"]["context"] == "context本文"
        assert bundle["data"]["playbook"] == "playbook本文"
        assert bundle["data"]["activity_id"] == 1234
        assert bundle["data"]["topic_id"] == 567
        # goal_text omit 時は task_title をフォールバック
        assert bundle["data"]["goal_text"] == "バンドル送信テスト"

    def test_spawn_passes_explicit_goal_text(
        self, _stub_spawn_environment, tmp_path: Path
    ):
        """goal_text を明示すると envelope と worker_cmd の両方に反映される"""
        result = ow_service.ow_spawn_worker(
            alias="w-bundle02",
            channel="ChAbCdEf",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="タイトル",
            acceptance="",
            context="",
            playbook="",
            task_n=7,
            goal_text="明示ゴール本文",
        )
        bundle = _stub_spawn_environment[1]
        assert bundle["data"]["goal_text"] == "明示ゴール本文"
        # worker_cmd の prompt にも /goal 経由で含まれる
        assert "/goal 明示ゴール本文" in result["command"]


class TestSpawnBundleSendFailure:
    def test_bundle_send_failure_aborts_spawn(self, monkeypatch, tmp_path: Path):
        """event:spawn-bundle 送信失敗時は SPAWN_PRECONDITION_FAILED で即 return する"""
        call_log: list[str] = []

        def _ow_send(channel, handle, body, needs_reply=False, in_reply_to=None):
            data_type = body.get("data", {}).get("type")
            state = body.get("data", {}).get("state")
            if data_type == "state" and state == "spawning":
                call_log.append("spawning")
                return {"msg_id": 1}
            if data_type == "spawn-bundle":
                call_log.append("spawn-bundle")
                return {"error": {"code": 500, "message": "relay down"}}
            return {"msg_id": 999}

        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: True)
        monkeypatch.setattr(ow_service, "_get_presence", lambda ch: [])
        monkeypatch.setattr(ow_service, "ow_get_identity", lambda ch, h: None)
        monkeypatch.setattr(ow_service, "_ensure_worker_askuser_deny", lambda c: None)
        monkeypatch.setattr(ow_service, "ow_send", _ow_send)
        monkeypatch.setenv("OW_TERMINAL", "manual")

        result = ow_service.ow_spawn_worker(
            alias="w-failure1",
            channel="ChAbCdEf",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="失敗テスト",
        )
        assert "error" in result
        assert result["error"]["code"] == "SPAWN_PRECONDITION_FAILED"
        assert any("spawn-bundle" in w for w in result["error"]["warnings"])
        # spawning + spawn-bundle の 2 回呼ばれて停止 (adapter には到達しない)
        assert call_log == ["spawning", "spawn-bundle"]


class TestWorkerCmdComposition:
    def test_worker_cmd_includes_env_identifiers(
        self, _stub_spawn_environment, tmp_path: Path
    ):
        """worker_cmd に OW_CHANNEL / OW_ALIAS / OW_TASK_N が含まれる (OW_TASK_FILE は無い)"""
        result = ow_service.ow_spawn_worker(
            alias="w-envinj01",
            channel="ChAbCdEf",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="env注入テスト",
            task_n=99,
        )
        cmd = result["command"]
        assert "OW_CHANNEL=" in cmd
        assert "OW_ALIAS=" in cmd
        assert "OW_TASK_N=" in cmd
        assert "OW_TASK_FILE=" not in cmd

    def test_worker_cmd_uses_goal_prompt_and_no_add_dir(
        self, _stub_spawn_environment, tmp_path: Path
    ):
        """worker_cmd の prompt は `/goal ...` 形式、`--add-dir` は含まれない"""
        result = ow_service.ow_spawn_worker(
            alias="w-promptest",
            channel="ChAbCdEf",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="プロンプトテスト",
        )
        cmd = result["command"]
        assert "/goal " in cmd
        assert "check_in" in cmd
        assert "--add-dir" not in cmd
        # 旧フォーマット (task: <path>) が含まれていない
        assert "task: /" not in cmd
