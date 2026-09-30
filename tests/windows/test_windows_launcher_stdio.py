"""src/launcher.py の stdio ブリッジがMCPクライアントと同じ経路で応答することを
確かめる。

Windowsの既定イベントループ(Proactor)では、launcherが`sys.stdin.buffer`を
`loop.connect_read_pipe`に渡す箇所(_stdin_reader_task)がWinError 6 →
AttributeErrorの連鎖で壊れ、stdinを1バイトも読めなくなる。launcher自体は
落ちずheartbeatを送り続けるため、MCPクライアント側からは`/mcp`が
Request timed outに見える。

ここではNode.jsのchild_process.spawn(pipe stdio)からlauncherを起動し、実際の
Claude Code起動経路と同じ形でJSON-RPCメッセージを送って検証する。

2本のテストに分けている:
- test_discover_responds_and_exits_cleanly: CALM_URLを「acceptを呼ばず接続だけ
  受け付け続けるTCPソケット」に向け、バックエンドが一切応答しない状態でも
  `server/discover`はlauncher自身が応答すること、stdinを閉じれば一定時間内に
  終了することを見る。ポート52837/52836に一切触れないため、CI・ローカルの
  どちらで実行しても既存のCALMサーバーに影響しない。
- test_full_roundtrip_against_local_server: ローカルモード(CALM_URL未設定)で
  実サーバー(src.main --transport http、ポート52837固定)を使い、
  discover/initialize/notifications initialized/tools list/tools callまで
  一通り確認する。ポートが固定でテスト側から変更できないため、CI環境
  (CI環境変数がある)でのみ実行する。開発者のマシンで既に52837/52836を
  使っている実サーバーへ相乗りしてしまう事故を避けるため。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from tests.windows.support import (
    REPO_ROOT,
    isolated_env,
    kill_pid,
    load_mcp_launcher_command,
    read_lock_file,
)

PROBE_SCRIPT = Path(__file__).resolve().parent / "scripts" / "launcher_probe.js"


def _node_available() -> bool:
    return shutil.which("node") is not None


def _run_probe(config: dict, tmp_path: Path) -> dict:
    config_path = tmp_path / "probe-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    result = subprocess.run(
        ["node", str(PROBE_SCRIPT), str(config_path)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.stdout.strip(), (
        f"launcher_probe.js produced no stdout (exit={result.returncode}). "
        f"stderr={result.stderr}"
    )
    last_line = result.stdout.strip().splitlines()[-1]
    try:
        return json.loads(last_line)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"launcher_probe.js stdout is not valid JSON: {result.stdout!r}\n"
            f"node stderr: {result.stderr}"
        ) from e


@pytest.mark.skipif(not _node_available(), reason="node is required to drive the launcher over stdio")
def test_discover_responds_and_exits_cleanly(tmp_path):
    """R1: バックエンドが一切応答しない状態でも、`server/discover`への応答と
    stdin close後の終了はlauncher自身のstdin読み取りにしか依存しない。
    """
    argv, mcp_env_overrides = load_mcp_launcher_command()

    # acceptを一切呼ばない「ブラックホール」リスナー: TCPの3way handshakeは
    # OSのbacklogが完了させるためconnect自体は成立するが、以後何も返らない。
    # これによりCALM_URLで指すHTTPバックエンドが「繋がるが一切応答しない」
    # 状態を作り、discover応答がstdin読み取り(launcher自身の処理)にしか
    # 依存しないことを保証する。既存のCALMサーバー(52837/52836)には一切触れない。
    blackhole = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blackhole.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blackhole.bind(("127.0.0.1", 0))
    blackhole.listen(5)
    port = blackhole.getsockname()[1]
    try:
        env = isolated_env(tmp_path)
        env.update(mcp_env_overrides)
        env["CALM_URL"] = f"http://127.0.0.1:{port}"
        env["CALM_LAUNCHER_MAX_RETRIES"] = "0"

        config = {
            "command": argv[0],
            "args": argv[1:],
            "cwd": str(REPO_ROOT),
            "env": env,
            "responseTimeoutMs": 15000,
            "exitTimeoutMs": 60000,
            "steps": [
                {
                    "name": "discover",
                    "method": "server/discover",
                    "id": 1,
                    "expectResponse": True,
                    "expectErrorCode": -32601,
                    "timeoutMs": 15000,
                },
            ],
        }
        result = _run_probe(config, tmp_path)
        assert result["ok"], (
            f"launcher stdio probe failed at stage={result.get('stage')}: "
            f"{result.get('error')}\nlauncher stderr:\n{result.get('stderr')}"
        )
        assert result["exitCode"] == 0, (
            f"launcher exited with code={result['exitCode']} signal={result.get('exitSignal')}\n"
            f"launcher stderr:\n{result.get('stderr')}"
        )
    finally:
        blackhole.close()


def _ci_env() -> bool:
    return bool(os.environ.get("CI"))


def _wellknown_ports_free() -> bool:
    for port in (52837, 52836):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return False
    return True


@pytest.mark.skipif(not _node_available(), reason="node is required to drive the launcher over stdio")
@pytest.mark.skipif(
    not _ci_env(),
    reason=(
        "local mode spawns the real server on the hardcoded port 52837/52836; "
        "only run this outside a developer's machine (CI env var gate)"
    ),
)
def test_full_roundtrip_against_local_server(tmp_path):
    """R1(discover/initialize/tools)をローカルモードの実サーバーで一通り確認する。

    ポート52837/52836はsrc側でハードコードされておりテストから変更できない
    ため、開発者のマシンで既に稼働中のCALMサーバーに相乗りする事故を避ける
    目的でCI環境変数がある場合にしか実行しない。念のため直前にも空きを確認する。
    """
    if not _wellknown_ports_free():
        pytest.skip("port 52837 or 52836 is already listening; refusing to spawn a local server")

    argv, mcp_env_overrides = load_mcp_launcher_command()
    env = isolated_env(tmp_path)
    env.update(mcp_env_overrides)
    env["CALM_LAUNCHER_MAX_RETRIES"] = "0"

    config = {
        "command": argv[0],
        "args": argv[1:],
        "cwd": str(REPO_ROOT),
        "env": env,
        "responseTimeoutMs": 20000,
        "exitTimeoutMs": 60000,
        "steps": [
            {
                "name": "discover",
                "method": "server/discover",
                "id": 1,
                "expectResponse": True,
                "expectErrorCode": -32601,
                "timeoutMs": 15000,
            },
            {
                "name": "initialize",
                "method": "initialize",
                "id": 2,
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "calm-windows-repro-test", "version": "0.0.1"},
                },
                "expectResponse": True,
                # 初回起動でuv syncやモデルロードが走る可能性があるため長めに待つ
                "timeoutMs": 90000,
            },
            {
                "name": "notifications/initialized",
                "method": "notifications/initialized",
                "expectResponse": False,
            },
            {
                "name": "tools/list",
                "method": "tools/list",
                "id": 3,
                "params": {},
                "expectResponse": True,
                "timeoutMs": 30000,
            },
            {
                "name": "tools/call",
                "method": "tools/call",
                "id": 4,
                # get_config: 引数不要・読み取り専用。searchはembeddingサーバーを
                # 起動しモデルを取得しうるため使わない。
                "params": {"name": "get_config", "arguments": {}},
                "expectResponse": True,
                "timeoutMs": 30000,
            },
        ],
    }

    try:
        result = _run_probe(config, tmp_path)
        assert result["ok"], (
            f"launcher stdio probe failed at stage={result.get('stage')}: "
            f"{result.get('error')}\nlauncher stderr:\n{result.get('stderr')}"
        )
        assert result["exitCode"] == 0, (
            f"launcher exited with code={result['exitCode']} signal={result.get('exitSignal')}\n"
            f"launcher stderr:\n{result.get('stderr')}"
        )
        # tools/callの応答本文に実際の値が乗っていることまで確認する
        call_resp = next(r["response"] for r in result["responses"] if r["step"] == "tools/call")
        assert "result" in call_resp, f"tools/call did not return a result: {call_resp}"
    finally:
        # launcherがローカルに自動起動したサーバーはstart_new_session(POSIX)/
        # デタッチ(Windows)で寿命が分離されており、launcher終了だけでは
        # 一緒に終わらない。一時HOME配下のlockファイルからpidを読んで個別にkillする。
        lock_info = read_lock_file(tmp_path)
        if lock_info is not None:
            kill_pid(lock_info["pid"])
        # embeddingサーバーは今回の対象ツール(get_config)では起動しないはずだが、
        # 万一起動していた場合に備えて一時HOME配下のプロセス台帳は残さない
        # (embedding_serverはlock機構を持たないため、ここでは明示的な後始末対象なし)。
