"""relay自己修復のE2E（実プロセスを伴う統合テスト）

シナリオ:
- /health を404で返す「旧版相当」のダミーHTTPサーバーを別ポートで起動して
  ポートを占有させる
- ow_service.RELAY_URL を同ポートに差し替える
- ensure_relay_server() を呼ぶと
  - _get_relay_health → None（ダミーが404を返すため）
  - _clear_relay_port → lsofでダミーのPIDを特定して kill
  - _start_relay_server → 本物の src/relay/server.py を同ポートで起動
  - _wait_for_relay_health → 新版が PROTOCOL_VERSION 一致のhealth dictを返す
  - 結果 True
- 最終クリーンアップとして起動したrelayプロセスをkill

ポートは socket.bind(0) で空きを取得し、RELAY_PORT env var で server.py に伝播する。
既存の8765上の本番relayと衝突しないようにするための処置。
"""
import json
import select
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from src.relay import PROTOCOL_VERSION
from src.services import ow_service


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _pick_free_port() -> int:
    """OSに空きポートを払い出してもらう。bind→getsockname→closeで即解放。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_legacy_stub_subprocess(port: int) -> subprocess.Popen:
    """旧版相当の404スタブを別Pythonプロセスとして起動する。

    別プロセスにする理由: ow_service._find_port_owners はlsofでLISTEN中のPIDを取得する。
    同プロセス内のスレッド起動ではPID = 自プロセスになり、ensure_relay_server が
    テストランナーを kill してしまう。
    """
    code = f"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({{"error": "not found"}}).encode("utf-8"))

    def do_POST(self):
        self.do_GET()

    def log_message(self, format, *args):
        return


srv = ThreadingHTTPServer(("127.0.0.1", {port}), H)
print("ready", flush=True)
srv.serve_forever()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # "ready"が来るまで待つ（最大3秒）
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        r, _, _ = select.select([proc.stdout], [], [], 0.05)
        if r:
            line = proc.stdout.readline()
            if line.strip() == "ready":
                return proc
    proc.kill()
    raise RuntimeError("legacy stub did not become ready in time")


def _wait_until_port_free(port: int, timeout: float = 5.0) -> bool:
    """ポートが解放されるまで待つ（kill直後のTIME_WAIT等を考慮）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _kill_pids_on_port(port: int) -> None:
    """テスト終了時のクリーンアップ: 指定ポートのLISTENプロセスを全kill。"""
    pids = ow_service._find_port_owners(port)
    for pid in pids:
        ow_service._kill_relay(pid)


@pytest.fixture
def isolated_relay(tmp_path, monkeypatch):
    """RELAY_URL / RELAY_PORT / RELAY_DB をテスト用に隔離する。

    - 空きポート: socket.bind(0)で確保
    - RELAY_DB: tmp_path配下に分離（本番relay.dbと無関係に）
    - RELAY_LOCK_PATH: tmp_path配下に分離（テスト並列時の競合を防ぐ）
    """
    port = _pick_free_port()
    relay_url = f"http://127.0.0.1:{port}"
    relay_db = str(tmp_path / "relay.db")

    monkeypatch.setattr(ow_service, "RELAY_URL", relay_url)
    monkeypatch.setattr(ow_service, "_RELAY_STATE_DIR", tmp_path)
    monkeypatch.setattr(ow_service, "_RELAY_LOCK_PATH", tmp_path / "relay.lock")
    # `_start_relay_server` は os.environ.copy() に RELAY_PORT を明示してから Popen に渡す。
    # monkeypatch.setenv はここで os.environ を書き換えるため、Popen 呼び出し時のコピーに反映される。
    monkeypatch.setenv("RELAY_PORT", str(port))
    monkeypatch.setenv("RELAY_DB", relay_db)

    yield port

    # cleanup: テストで起動したrelayプロセスを全部kill
    _kill_pids_on_port(port)
    _wait_until_port_free(port, timeout=3.0)


def _http_get(url: str, timeout: float = 2.0) -> tuple[int, dict | None]:
    """簡易GET。(status, json_or_None) を返す。"""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                return resp.status, json.loads(resp.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                return resp.status, None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None


def test_ensure_relay_server_kills_legacy_stub_and_starts_new(isolated_relay):
    """E2E: /health 404を返す旧版相当のスタブがport占有 → ensure_relay_serverが
    そのプロセスをkill→本物のsrc.relay.serverを同portで起動→新版が version一致を返す。

    Acceptance(1)(2): /health 404を返す旧版relayがportを占有しているとき、
    新版が占有プロセスを特定してkillし起動できること、および ensure_relay_server の
    自己修復が「旧版居座り」ケースで成立すること、をE2Eで検証する。
    """
    port = isolated_relay

    # ① 旧版相当の404スタブを別プロセスでportにbind
    stub = _start_legacy_stub_subprocess(port)
    try:
        # /healthが404を返すことを確認（前提条件）
        status, _ = _http_get(f"http://127.0.0.1:{port}/health", timeout=1.0)
        assert status == 404, "legacy stubが404を返していない（テストの前提崩壊）"

        # ② ensure_relay_server を呼ぶ → self-healで新版が立ち上がるはず
        ok = ow_service.ensure_relay_server()
        assert ok is True, "ensure_relay_serverがTrueを返さなかった（self-heal失敗）"
    finally:
        # スタブ側はもうkillされているはずだが念のため
        try:
            stub.kill()
        except Exception:
            pass

    # ③ 新版relayが応答していることを /health で確認
    status, payload = _http_get(f"http://127.0.0.1:{port}/health", timeout=2.0)
    assert status == 200, f"新版relayが200を返さない: status={status} payload={payload}"
    assert isinstance(payload, dict)
    assert payload.get("protocol_version") == PROTOCOL_VERSION, (
        f"新版relayのprotocol_versionが一致しない: {payload}"
    )

    # ④ 元のスタブPIDは生きていないこと
    assert stub.poll() is not None, "旧版スタブが残存している（killされていない）"


def test_ensure_relay_server_returns_true_when_already_healthy(isolated_relay):
    """E2E: 何も立っていないportに対して ensure_relay_server を呼ぶと
    新版を素直に起動して True を返す（port占有なしのbaselineケース）。
    """
    port = isolated_relay

    # スタブも本物も立てていない状態でensure_relay_serverを呼ぶ
    ok = ow_service.ensure_relay_server()
    assert ok is True

    # 起動した新版が一致するversionを返す
    status, payload = _http_get(f"http://127.0.0.1:{port}/health", timeout=2.0)
    assert status == 200
    assert payload is not None
    assert payload.get("protocol_version") == PROTOCOL_VERSION
