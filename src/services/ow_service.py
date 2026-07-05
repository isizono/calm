"""ow v1メッセージングサービス

relay HTTPサーバーとのやり取り（ow_send / ow_history）と、relayサーバーの
起動・自己修復を担う。外部HTTPのためcc-memory DBのconn共有パターンは不要。
urllib.requestベース（サードパーティ依存なし）。

relayサーバーはcc-memoryリポ内のsrc/relay/にvendoringされており、ow_serviceと
PROTOCOL_VERSIONを構造的に共有する。ensure_relay_serverは/healthのversion不一致時に
古いrelayをkillして再起動する自己修復gate。
"""
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from src.relay import PROTOCOL_VERSION

logger = logging.getLogger(__name__)

# ----------------------------
# 設定定数
# ----------------------------

RELAY_URL = os.environ.get("RELAY_URL", "http://127.0.0.1:8765")

# cc-memoryリポルート（src/services/ow_service.py → src/ → repo root）
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# vendoring済みrelayサーバー（src/relay/server.py）。env overrideでforkに切替可能だがデフォルトは固定。
RELAY_DIR = os.environ.get("RELAY_DIR", str(_REPO_ROOT / "src" / "relay"))

# 自己修復用ロック（relay起動/kill時の競合防止）とDBパス
_RELAY_STATE_DIR = Path.home() / ".cc-memory" / "ow" / "relay"
_RELAY_LOCK_PATH = _RELAY_STATE_DIR / "relay.lock"

_MAX_RETRIES = 3

# ----------------------------
# relay HTTPヘルパー
# ----------------------------


def _relay_request(method: str, path: str, data: dict | None = None) -> dict:
    """relay HTTPリクエストの共通ヘルパー。4xx即失敗、5xx/接続断のみリトライ。

    Args:
        method: "GET" or "POST"
        path: relayエンドポイントパス（例: "/send", "/history?channel=...&since=0"）
        data: POSTボディ（Noneの場合はGET）

    Returns:
        レスポンスJSON dictまたは {"error": ...}
    """
    url = f"{RELAY_URL}{path}"
    body_bytes = json.dumps(data).encode("utf-8") if data is not None else None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body_bytes,
                headers={"Content-Type": "application/json"} if body_bytes else {},
                method=method,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code < 500:
                # 4xx: クライアントエラー → 即失敗（リトライしない）
                try:
                    err_body = json.loads(e.read())
                except Exception:
                    err_body = {"message": str(e)}
                return {"error": {"code": e.code, "message": err_body}}
            if attempt >= _MAX_RETRIES:
                raise
            sleep_secs = 2 ** attempt
            logger.warning(
                "relay %s %s returned %d, retrying in %ds (%d/%d)",
                method, path, e.code, sleep_secs, attempt + 1, _MAX_RETRIES,
            )
            time.sleep(sleep_secs)
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            if attempt >= _MAX_RETRIES:
                raise
            sleep_secs = 2 ** attempt
            logger.warning(
                "relay %s %s failed: %s, retrying in %ds (%d/%d)",
                method, path, e, sleep_secs, attempt + 1, _MAX_RETRIES,
            )
            time.sleep(sleep_secs)

    # ここには到達しない（ループ内でraiseされる）
    raise RuntimeError("unreachable")


# ----------------------------
# relayサーバー起動確認・自己修復
# ----------------------------


def _get_relay_health() -> dict | None:
    """GET /healthでrelayの生死＋PROTOCOL_VERSION含むdictを取得する。

    返り値:
        - dict: relay稼働中。`protocol_version`/`pid`/`status`等を含む
        - None: relay未起動・応答なし・/health非対応（旧版相当）

    旧 `_is_relay_running` は404でもTrueを返す設計だったため、改名前の古いrelayが
    動いていても「running」と誤判定して新規spawnを諦めていた。本関数はversion不一致時に
    呼び出し元（ensure_relay_server）がkill+restartで自己修復できるよう
    /healthレスポンスをそのまま返す。
    """
    try:
        req = urllib.request.Request(f"{RELAY_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status != 200:
                return None
            try:
                data = json.loads(resp.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
            if not isinstance(data, dict):
                return None
            return data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # /health非対応の旧relayが応答している = 互換性なし。Noneで「未起動扱い」にしてrestartへ
            logger.warning("relay returned 404 for /health (legacy server detected)")
            return None
        return None
    except Exception:
        return None


def _start_relay_server() -> bool:
    """relayサーバーをバックグラウンドで起動する（vendoring済みリポ内固定パス）。

    `python -m src.relay.server` で起動する。RELAY_DBは固定パス（server.py側のデフォルト）に
    委ねるが、env override（RELAY_URL/RELAY_DIR）が指定されていればそれを尊重する。
    """
    relay_dir = Path(RELAY_DIR).expanduser()
    server_py = relay_dir / "server.py"
    if not server_py.exists():
        logger.warning("relay server.py not found at %s", server_py)
        return False
    # vendoring済みのリポ内パッケージとして起動する（`python -m src.relay.server`）。
    # `_REPO_ROOT/src/relay/server.py` が標準で、env override時はファイル直接実行にフォールバック。
    use_module = (server_py.resolve() == (_REPO_ROOT / "src" / "relay" / "server.py").resolve())
    if use_module:
        cmd = [sys.executable, "-m", "src.relay.server"]
        cwd = str(_REPO_ROOT)
    else:
        cmd = [sys.executable, str(server_py)]
        cwd = str(relay_dir)
    env = os.environ.copy()
    port = _get_relay_port()
    if port:
        env["RELAY_PORT"] = str(port)
    try:
        subprocess.Popen(
            cmd,
            cwd=cwd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        logger.info("relay server process started (cmd=%s)", cmd)
        return True
    except OSError as e:
        logger.warning("Failed to start relay server: %s", e)
        return False


def _get_relay_port() -> int | None:
    """RELAY_URL から待受ポート番号を抽出する。

    `http://127.0.0.1:8765` のような形式から 8765 を取り出す。
    解析失敗時は None（_find_port_owners 側でスキップ）。
    """
    try:
        parsed = urllib.parse.urlparse(RELAY_URL)
        return parsed.port
    except (ValueError, TypeError):
        return None


# `lsof unavailable` warningは ensure_relay_server 呼び出しの度に出ると煩いため、
# プロセスライフタイムで1回だけ警告する（以降は debug ログに格下げ）。
_lsof_unavailable_logged = False


def _find_port_owners(port: int) -> list[int]:
    """指定ポートを LISTEN 中のプロセスPIDを返す（lsof経由）。

    `/health` 404 を返す旧版relayや、何らかの別プロセスがポートを占有しているケースで、
    `_start_relay_server` 前にそのPIDを特定してkillするために使う。
    lsofが存在しない・実行失敗・占有プロセス無しはいずれも空リストを返し、呼び出し元は
    そのまま起動を試みて従来挙動（bind失敗で起動失敗扱い）にフォールバックする。

    macOS/Linuxを想定し、`lsof -ti:<port> -sTCP:LISTEN` で LISTEN 限定のPIDのみ取得する
    （curl等のクライアント接続側PIDを含めないため）。

    Note:
        lsof必須。Alpine等のlsof不在環境ではこの自己修復経路は無効化され、
        `ensure_relay_server` は `_start_relay_server` のbind失敗 → wait timeout → False
        の従来挙動に落ちる（その場合 `RELAY_UNAVAILABLE` が呼び出し元に伝搬する）。
        コンテナ運用する場合は `lsof` を含むベースイメージ（Debian slim等）の利用を推奨する。
    """
    global _lsof_unavailable_logged
    timeout_sec = 2  # lsofは通常ms単位、_wait_for_relay_healthのtimeout(10s)と接近させない
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        if not _lsof_unavailable_logged:
            logger.warning(
                "lsof unavailable for port %s: %s (relay self-heal port-clear disabled; further occurrences logged at debug)",
                port, e,
            )
            _lsof_unavailable_logged = True
        else:
            logger.debug("lsof unavailable for port %s: %s", port, e)
        return []

    # lsof は該当プロセスがない場合 exit code 1 + 空 stdout
    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _clear_relay_port() -> int:
    """RELAY_URL のポート占有プロセスを全てkillする。killしたPID数を返す。

    `ensure_relay_server` の `health is None` 枝で `_start_relay_server` 前に呼ばれる。
    /health 非対応の旧版relayや無関係プロセスが居座っているケースを self-heal するための経路。
    `_kill_relay` がSIGTERM→SIGKILL fallbackと例外握り潰しを担うため、ここでは単純に列挙して
    順次killし、bind可能な状態を作るだけに専念する。

    WARNING:
        本関数は `RELAY_URL` のportを LISTEN 中の**任意の**プロセスを kill する。
        プロセス種別の検証は行わない（lsofで返ってきたPIDをそのまま `_kill_relay` に渡す）。
        したがって `RELAY_URL` の port は relay 専用の値（デフォルト 8765）を使うこと。
        誤って他サービスと共有しているportを指定すると、そのサービスを巻き込んで kill する。
        実害は kill 権限の範囲（同一ユーザー）に閉じるが、設計上の前提として明示しておく。
    """
    port = _get_relay_port()
    if port is None:
        return 0
    pids = _find_port_owners(port)
    if not pids:
        return 0
    logger.warning(
        "clearing %d stale process(es) holding relay port %d (pids=%s) before restart",
        len(pids), port, pids,
    )
    for pid in pids:
        _kill_relay(pid)
    return len(pids)


def _kill_relay(pid: int) -> None:
    """relayプロセスにSIGTERM→数秒待機→SIGKILLでkillする。

    版不一致のrelayが動いているケースで呼ばれる。SIGTERM後にpoll間隔0.1秒x20回（最大2秒）で
    終了を待ち、それでも生存していればSIGKILL。プロセス不在/権限不足は致命的でないため
    ログして握り潰す（ポートが空けば次の_start_relay_serverが成功する）。
    """
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        logger.warning("permission denied when SIGTERMing relay pid=%s", pid)
        return
    except OSError as e:
        logger.warning("SIGTERM failed for pid=%s: %s", pid, e)
        return

    for _ in range(20):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)  # 生存確認
        except ProcessLookupError:
            return
        except OSError:
            return

    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError) as e:
        logger.warning("SIGKILL fallback failed for pid=%s: %s", pid, e)


def _open_relay_lock():
    """relay起動/kill区間を排他するためのflock fdを開いて返す（要close）。

    複数orch/workerが同時にensure_relay_serverを呼んでもkill＋start中の競合（lost update的なrace）が
    起きないよう、`~/.cc-memory/ow/relay/relay.lock` をflock(LOCK_EX)で排他する。
    最悪二重起動になってもポートbindで2つ目が即死するため致命的ではないが、kill直後にもう一つの
    プロセスがhealth=正常と判定して何もしない、というすれ違いを防ぐ。
    """
    _RELAY_STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_RELAY_LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _close_relay_lock(fd) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _wait_for_relay_health(timeout_sec: float = 10.0, interval_sec: float = 0.5) -> dict | None:
    """relayの/healthが返るまでポーリング。timeout内に揃ったhealth dictを返す。"""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        health = _get_relay_health()
        if health is not None:
            return health
        time.sleep(interval_sec)
    return None


def ensure_relay_server() -> bool:
    """relayの起動・互換性gateを満たすことを保証する自己修復関数。

    フロー:
      1. flock排他（kill+restart区間のみ）
      2. /health取得
         - 応答なし → 起動して/health安定まで待つ
         - protocol_version != PROTOCOL_VERSION → 古いrelayをkillして起動し直す
         - 一致 → そのまま成功
    Returns:
        Trueなら接続可能、Falseなら起動失敗・互換版立ち上がりに失敗
    """
    lock_fd = _open_relay_lock()
    try:
        health = _get_relay_health()
        if health is None:
            # 未起動 or 旧版404扱い。「/health 404 を返す旧版relayがport占有中」のケースでは
            # 起動前にport占有プロセスをkillしないとEADDRINUSEで新版が起動できない。
            # 占有が無い未起動ケースでは _clear_relay_port が0件で何もせず通過する。
            _clear_relay_port()
            if not _start_relay_server():
                return False
            health = _wait_for_relay_health()
            if health is None:
                # 起動直後に/healthが揃わない原因の最頻ケースはbind失敗（並行起動・占有再発）。
                # 一度だけport占有を再掃除→再起動する（self-heal 1回限り）。リトライしても揃わなければFalse。
                # flock保持中なので別orch/workerからの並行起動は発生せず、無限ループにはならない。
                # clear件数が0でもリトライする（_start_relay_serverが起動途中で死んだ等のレアケースを救うため）。
                logger.warning(
                    "relay /health did not converge — clearing port and retrying once (cleared=%d)",
                    _clear_relay_port(),
                )
                if not _start_relay_server():
                    return False
                health = _wait_for_relay_health()
                if health is None:
                    logger.warning("relay server failed to start within timeout (after retry)")
                    return False
        elif health.get("protocol_version") != PROTOCOL_VERSION:
            # 版不一致: 古いrelayをkill→新版を起動
            stale_pid = health.get("pid")
            logger.warning(
                "relay protocol_version mismatch (running=%s, expected=%s, pid=%s) — restarting",
                health.get("protocol_version"), PROTOCOL_VERSION, stale_pid,
            )
            if isinstance(stale_pid, int):
                _kill_relay(stale_pid)
            if not _start_relay_server():
                return False
            health = _wait_for_relay_health()
            if health is None or health.get("protocol_version") != PROTOCOL_VERSION:
                logger.warning(
                    "relay restart did not converge to PROTOCOL_VERSION=%s (got=%s)",
                    PROTOCOL_VERSION, health,
                )
                return False
        return True
    finally:
        _close_relay_lock(lock_fd)


def _is_relay_running() -> bool:
    """後方互換ラッパー（テスト用）。`_get_relay_health()` is not None と等価。"""
    return _get_relay_health() is not None


def ensure_channel(channel_code: str) -> bool:
    """channelが存在しなければrelayに作成する（idempotent）。

    POST /createにchannel_codeを指定して送信する。relayサーバー側で
    既存なら何もせず、未存在なら作成する。

    Args:
        channel_code: 存在を保証したいchannel_code

    Returns:
        True: channelが存在する（作成成功・既存どちらも）
        False: 作成失敗（4xx・5xx・接続断すべて含む）
    """
    try:
        result = _relay_request("POST", "/create", {"channel_code": channel_code})
    except Exception as e:
        logger.warning("ensure_channel failed for %s: %s", channel_code, e)
        return False
    if "error" in result:
        logger.warning("ensure_channel failed for %s: %s", channel_code, result["error"])
        return False
    logger.info("ensure_channel: channel %s is ready", channel_code)
    return True


# ----------------------------
# ow_send
# ----------------------------


def _maybe_inject_term_ref(body: dict) -> dict:
    """identity event の term_ref をファイルキャッシュから補完する。

    SessionStart hook (hooks/term_ref_cache.py) が worker shell の env を
    `~/.cc-memory/ow/term_refs/<session_id>.json` に書き出している前提。
    body が identity event で term_ref 未設定なら session_id でキャッシュを引いて補完する。

    lookup 失敗時は body をそのまま返す（補完失敗時は素通し）。
    元の body / data dict は破壊せず、補完時のみ shallow copy で新 dict を返す。
    """
    if not isinstance(body, dict) or body.get("kind") != "event":
        return body
    data = body.get("data")
    if not isinstance(data, dict):
        return body
    if data.get("type") != "identity":
        return body
    if data.get("term_ref"):
        return body
    session_id = data.get("session_id")
    if not session_id:
        return body
    cache_path = Path.home() / ".cc-memory" / "ow" / "term_refs" / f"{session_id}.json"
    try:
        with cache_path.open(encoding="utf-8") as f:
            cached = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return body
    term_ref = cached.get("term_ref") if isinstance(cached, dict) else None
    if not term_ref:
        return body
    new_data = dict(data)
    new_data["term_ref"] = term_ref
    new_body = dict(body)
    new_body["data"] = new_data
    return new_body


def ow_send(
    channel: str,
    handle: str,
    body: dict,
    needs_reply: bool = False,
    in_reply_to: int | None = None,
) -> dict:
    """ow channelにメッセージを送信する。

    bodyはow固有JSONを格納するdict（relay schemaは無改修）。
    4xx即失敗、5xx/接続断のみ3回指数バックオフ。
    channel未存在（404）の場合はensure_channelで自動作成してから再送する。

    Args:
        channel: channelコード
        handle: 送信者handle（例: "orch", "w-a"）
        body: ow固有JSON（{"v":1, "kind":"command"|"event", ...}）
        needs_reply: 返信を期待するか
        in_reply_to: 返信先のmsg_id

    Returns:
        成功時: {"msg_id": int}
        失敗時: {"error": {...}}
    """
    body = _maybe_inject_term_ref(body)
    payload: dict = {
        "channel": channel,
        "handle": handle,
        "body": json.dumps(body),
        "needs_reply": needs_reply,
    }
    if in_reply_to is not None:
        payload["in_reply_to"] = in_reply_to

    result = _relay_request("POST", "/send", payload)

    # channel未存在による404 → 自動作成して再送（1回のみ）
    if "error" in result and result["error"].get("code") == 404:
        logger.info("ow_send: channel %s not found, attempting ensure_channel", channel)
        if ensure_channel(channel):
            result = _relay_request("POST", "/send", payload)

    return result


# ----------------------------
# ow_history
# ----------------------------


def ow_history(channel: str, since: int = 0, limit: int = 100) -> dict:
    """GET /history。受信処理の本体。

    since自身を含まない（msg_id > since）。

    Args:
        channel: channelコード
        since: このmsg_idより大きいものを返す（0=全件）
        limit: 最大取得件数

    Returns:
        {"messages": [{"msg_id": int, "handle": str, "body": dict, ...}, ...]}
        失敗時: {"error": {...}}
    """
    params = urllib.parse.urlencode({"channel": channel, "since": since, "limit": limit})
    path = f"/history?{params}"
    result = _relay_request("GET", path)
    if "error" in result:
        return result

    # bodyがJSON文字列の場合はパースする
    messages = result.get("messages", [])
    for msg in messages:
        if isinstance(msg.get("body"), str):
            try:
                msg["body"] = json.loads(msg["body"])
            except (json.JSONDecodeError, TypeError):
                pass  # パース失敗はそのまま文字列で返す

    return {"messages": messages}
