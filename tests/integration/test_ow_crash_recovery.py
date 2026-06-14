"""ow crash復旧自動化のintegrationテスト (T17)

シナリオ:
    1. relayサーバーを動的portで実プロセスとして起動（テスト分離のため別portを使う）
    2. 複数workerのstate宣言（ready→working→done等）をrelayへ実送信し、履歴を蓄積
    3. queueファイルを物理的に作成して「crash前の状態」を再現
    4. presenceは「全worker offline」「特定workerがonline」等を制御し、3者突合の組合せを検証
    5. ow_recoverを呼び、queue自動更新・ping送信が正しく実行されることを検証

実relayサーバープロセスとファイルI/Oを使う＝「マルチプロセスでcrash→再起動→突合シナリオ」の acceptance を満たす。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from src.services import ow_service


def _free_port() -> int:
    """OSに空きportを払い出してもらう（テスト間の衝突回避）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_until_healthy(url: str, timeout_sec: float = 10.0) -> bool:
    """与えられたbase_urlの/healthが200を返すまでpollする。"""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(f"{url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


@pytest.fixture
def live_relay(tmp_path, monkeypatch):
    """別portでrelayサーバープロセスを起動し、RELAY_URLを差し替える。

    DB/lockもtmp_pathに隔離して既存稼働中のrelayと衝突しないようにする。
    """
    repo_root = Path(__file__).resolve().parents[2]
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "relay.db"

    # 別portで起動するため、PORT属性を上書きするブートストラップコードを-cで渡す
    bootstrap = (
        f"import sys; sys.path.insert(0, {repr(str(repo_root))}); "
        f"from src.relay import server; server.PORT = {port}; "
        f"server.main({repr(str(db_path))})"
    )

    env = os.environ.copy()
    env["RELAY_DB"] = str(db_path)

    proc = subprocess.Popen(
        [sys.executable, "-c", bootstrap],
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        if not _wait_until_healthy(base_url, timeout_sec=10.0):
            stdout, stderr = proc.communicate(timeout=2)
            pytest.fail(
                f"relay did not become healthy on port {port}. "
                f"stdout={stdout!r} stderr={stderr!r}"
            )

        # ow_service側のURL／state dirをテスト用に差し替え
        monkeypatch.setattr(ow_service, "RELAY_URL", base_url)
        state_dir = tmp_path / "ow-state"
        state_dir.mkdir()
        monkeypatch.setattr(ow_service, "_RELAY_STATE_DIR", state_dir)
        monkeypatch.setattr(ow_service, "_RELAY_LOCK_PATH", state_dir / "relay.lock")

        yield {
            "url": base_url,
            "port": port,
            "proc": proc,
            "tmp_path": tmp_path,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def _send_state(channel: str, alias: str, task: str, state: str):
    """workerからの state 宣言を実relayに送信する。"""
    body = {
        "v": 1, "kind": "state", "from": alias, "to": "orch",
        "task": task, "state": state, "data": {},
    }
    result = ow_service.ow_send(channel=channel, handle=alias, body=body)
    assert "msg_id" in result, f"send failed: {result}"
    return result["msg_id"]


def _setup_queue(tmp_path: Path, topic_id: str, content: str) -> Path:
    """queueファイルを物理的に作成し、OW_QUEUE_DIRを向ける。"""
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir(exist_ok=True)
    (queue_dir / f"queue-t{topic_id}.md").write_text(content)
    return queue_dir


class TestCrashRecoveryScenario:
    """worker死亡→orch再起動→ow_recoverによる自動修復のフル統合シナリオ。"""

    def test_ghost_active_recovered_from_relay_history(self, live_relay, monkeypatch):
        """シナリオ: 2workerが working/done を送って消滅 → ow_recoverでqueueが再構築される

        前提:
            - w-a が T1 で ready→working を送信、その後crash（presence offline）
            - w-b が T2 で ready→working→done を送信、その後消滅
            - queueファイルは crash 前の状態 (両方working) のまま
        期待:
            - w-a: relay最新=working & offline → queue=stalled
            - w-b: relay最新=done & offline → queue=done
        """
        channel = "TestT17a"
        tmp_path = live_relay["tmp_path"]

        # relayにworker stateを蓄積
        ow_service.ensure_channel(channel)
        _send_state(channel, "w-a", "T1", "ready")
        _send_state(channel, "w-a", "T1", "working")
        _send_state(channel, "w-b", "T2", "ready")
        _send_state(channel, "w-b", "T2", "working")
        _send_state(channel, "w-b", "T2", "done")

        # crash前のqueueファイル状態（両方workingで残留）
        queue_dir = _setup_queue(
            tmp_path, "999",
            "## T1 | task-a | working\n"
            "- worker: w-a / term_ref: ta / session: sa\n"
            "- note: crash前\n"
            "\n"
            "## T2 | task-b | working\n"
            "- worker: w-b / term_ref: tb / session: sb\n"
            "- note: crash前\n",
        )
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))

        # presence: 全worker offline (relay接続は send だけで切ったため)
        result = ow_service.ow_recover(channel=channel, topic_id="999")

        # 2件のghost_activeを検出して両方更新
        assert result["dry_run"] is False
        assert len(result["detected"]["ghost_active"]) == 2
        applied_tasks = {u["task"]: u["to"] for u in result["applied"]["queue_updates"]}
        assert applied_tasks == {"T1": "stalled", "T2": "done"}

        # queueファイルが書き換わっていること
        new_content = (queue_dir / "queue-t999.md").read_text()
        assert "## T1 | task-a | stalled\n" in new_content
        assert "## T2 | task-b | done\n" in new_content
        # noteも更新されている
        assert "crash-recovery" in new_content

    def test_orphan_worker_triggers_ping(self, live_relay, monkeypatch):
        """シナリオ: queueに登場しないworkerがpresence onlineで残存 → orphan ping

        マルチプロセス: 別プロセスで SSE接続して presence登録した状態を作る。
        """
        channel = "TestT17b"
        tmp_path = live_relay["tmp_path"]
        ow_service.ensure_channel(channel)
        _send_state(channel, "w-z", "T9", "ready")

        # presence登録: 別プロセスでSSE接続を張る (短時間)
        # SSE接続: stdout/stderrはDEVNULL（PIPEだとSSEのpayload蓄積でパイプバッファ溢れ→
        # 子プロセスのwrite blockingでterminateが遅延する。C2対応）
        ssec = subprocess.Popen(
            [
                sys.executable, "-c",
                f"import urllib.request; "
                f"req = urllib.request.Request('{live_relay['url']}/stream?channel={channel}&handle=w-z'); "
                f"resp = urllib.request.urlopen(req, timeout=10); "
                f"import time; time.sleep(15)",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            # presence登録が反映されるまで最大10秒待つ（CI環境を考慮）
            registered = False
            for _ in range(100):
                if "w-z" in ow_service._get_presence(channel):
                    registered = True
                    break
                time.sleep(0.1)
            assert registered, "presence registration timed out"

            # queueはT9を含まない（orphan扱い）
            queue_dir = _setup_queue(
                tmp_path, "998",
                "## T1 | task-a | working\n- worker: w-a / term_ref: ta / session: sa\n",
            )
            monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))

            result = ow_service.ow_recover(channel=channel, topic_id="998")
            assert any(o["alias"] == "w-z" for o in result["detected"]["orphans"])
            # pingが送信されている
            pings = result["applied"]["pings_sent"]
            assert any(p["alias"] == "w-z" and p["reason"] == "orphan" for p in pings)

            # 実relayの履歴にpingメッセージが残っていることを確認（end-to-end）
            history = ow_service.ow_history(channel)
            ping_msgs = [
                m for m in history["messages"]
                if isinstance(m.get("body"), dict)
                and m["body"].get("kind") == "cmd"
                and m["body"].get("verb") == "ping"
                and m["body"].get("to") == "w-z"
            ]
            assert ping_msgs, "ping was not actually delivered to relay"
        finally:
            ssec.terminate()
            try:
                ssec.wait(timeout=3)
            except subprocess.TimeoutExpired:
                ssec.kill()
                ssec.wait(timeout=2)

    def test_dry_run_against_live_relay_makes_no_writes(self, live_relay, monkeypatch):
        """dry_run=True なら実relayに対する突合検出は走るが書き込みは一切起きない"""
        channel = "TestT17c"
        tmp_path = live_relay["tmp_path"]
        ow_service.ensure_channel(channel)
        _send_state(channel, "w-a", "T1", "done")

        queue_dir = _setup_queue(
            tmp_path, "997",
            "## T1 | task-a | working\n- worker: w-a / term_ref: ta / session: sa\n",
        )
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))

        original = (queue_dir / "queue-t997.md").read_text()
        result = ow_service.ow_recover(channel=channel, topic_id="997", dry_run=True)

        # 検出はある
        assert len(result["detected"]["ghost_active"]) == 1
        assert result["detected"]["ghost_active"][0]["suggested_status"] == "done"
        # 適用はゼロ
        assert result["applied"]["queue_updates"] == []
        assert result["applied"]["pings_sent"] == []
        # queueファイル無変更
        assert (queue_dir / "queue-t997.md").read_text() == original


class TestSpawnPreflightAgainstLiveRelay:
    """spawn前ヘルスチェックを実relayに対して動かす。"""

    def test_alias_collision_detected_via_real_presence(self, live_relay, monkeypatch, tmp_path):
        """presenceに既にonlineのhandleがいる状態 → SPAWN_PRECONDITION_FAILED"""
        channel = "TestT17d"
        ow_service.ensure_channel(channel)

        # C2対応: stdout/stderrはDEVNULL
        ssec = subprocess.Popen(
            [
                sys.executable, "-c",
                f"import urllib.request; "
                f"req = urllib.request.Request('{live_relay['url']}/stream?channel={channel}&handle=w-x'); "
                f"resp = urllib.request.urlopen(req, timeout=10); "
                f"import time; time.sleep(15)",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            registered = False
            for _ in range(100):
                if "w-x" in ow_service._get_presence(channel):
                    registered = True
                    break
                time.sleep(0.1)
            assert registered, "presence registration timed out"

            preflight = ow_service._validate_spawn_preconditions(
                alias="w-x", channel=channel, cwd=str(tmp_path),
            )
            assert preflight["ok"] is False
            assert any("alias w-x" in w for w in preflight["warnings"])
        finally:
            ssec.terminate()
            try:
                ssec.wait(timeout=3)
            except subprocess.TimeoutExpired:
                ssec.kill()
                ssec.wait(timeout=2)

    def test_cwd_missing_blocks_against_live_relay(self, live_relay, tmp_path):
        """relay/channel生きてもcwd不在ならprecondition_failed"""
        channel = "TestT17e"
        ow_service.ensure_channel(channel)
        missing = tmp_path / "no-such-dir"
        preflight = ow_service._validate_spawn_preconditions(
            alias="w-fresh", channel=channel, cwd=str(missing),
        )
        assert preflight["ok"] is False
        assert any("cwd" in w for w in preflight["warnings"])

    def test_all_clear_passes(self, live_relay, tmp_path):
        """relay生・channel作成成功・cwd存在・alias未使用 → ok=True"""
        channel = "TestT17f"
        ow_service.ensure_channel(channel)
        preflight = ow_service._validate_spawn_preconditions(
            alias="w-fresh", channel=channel, cwd=str(tmp_path),
        )
        assert preflight["ok"] is True
        assert preflight["warnings"] == []
