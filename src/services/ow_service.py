"""ow（orch/worker）基盤サービス

relay HTTPサーバーとのやり取り、worker spawn/close、ステータス管理を担う。
外部HTTPのためcc-memory DBのconn共有パターンは不要。urllib.requestベース（サードパーティ依存なし）。

relayサーバーはcc-memoryリポ内のsrc/relay/にvendoringされており、ow_serviceと
PROTOCOL_VERSIONを構造的に共有する。ensure_relay_serverは/healthのversion不一致時に
古いrelayをkillして再起動する自己修復gate。
"""
import errno
import fcntl
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.relay import PROTOCOL_VERSION

logger = logging.getLogger(__name__)

# ----------------------------
# 設定定数
# ----------------------------

RELAY_URL = os.environ.get("RELAY_URL", "http://127.0.0.1:8765")
OW_QUEUE_DIR = os.environ.get("OW_QUEUE_DIR", "")

# cc-memoryリポルート（src/services/ow_service.py → src/ → repo root）
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# vendoring済みrelayサーバー（src/relay/server.py）。env overrideでforkに切替可能だがデフォルトは固定。
RELAY_DIR = os.environ.get("RELAY_DIR", str(_REPO_ROOT / "src" / "relay"))

# 自己修復用ロック（relay起動/kill時の競合防止）とDBパス
_RELAY_STATE_DIR = Path.home() / ".cc-memory" / "ow" / "relay"
_RELAY_LOCK_PATH = _RELAY_STATE_DIR / "relay.lock"

_MAX_RETRIES = 3

# ----------------------------
# model validation / normalization
# ----------------------------


def _normalize_and_validate_model(model: str) -> tuple[str, str | None]:
    """model引数を正規化し、禁止モデルを拒否する。

    Returns:
        (正規化済みmodel, エラーメッセージ) のタプル。
        エラーなしの場合は (正規化済みmodel, None)。
    """
    m = model.lower().strip()

    # haiku系は worker での使用禁止
    if "haiku" in m:
        return "", (
            f"model '{model}' は worker では使用できません。"
            " haiku は SA (Agent ツール) での利用のみ許可されています。"
        )

    # opus-4-8 は禁止（恒久ルール）
    if "opus-4-8" in m or "opus4-8" in m:
        return "", (
            f"model '{model}' は使用できません。"
            " opus 4.8 は禁止されています。代わりに claude-opus-4-7 を使ってください。"
        )

    # sonnet系: [1m] が付いていなければ付与（バージョン固定なし）
    if "sonnet" in m:
        if "[1m]" not in m:
            return model + "[1m]", None
        return model, None

    # その他（opus含む）はそのまま透過
    return model, None


# reducer: v3 workload state 分類
_NON_TERMINAL_WORKLOAD_STATES: frozenset[str] = frozenset(
    {"loading", "ready", "working", "blocked", "escalated", "draining"}
)
# heartbeat タイムアウト閾値（周期×3）。
# キーは workload_state または heartbeat body の phase。両者は worker 側で同期される
# （event:state 送信時に heartbeat phase も追従）ため、同一の値空間として共有する。
_HEARTBEAT_TIMEOUT_SECS: dict[str, float] = {
    "loading": 30.0,  # 10s × 3
}
_HEARTBEAT_TIMEOUT_DEFAULT: float = 90.0  # 30s × 3（ready/working/draining共通）


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


# ----------------------------
# ow_spawn_worker / ow_close_worker
# ----------------------------


def _get_queue_dir() -> Path:
    """queueディレクトリパスを返す。

    OW_QUEUE_DIR環境変数が設定されていればそのパスを使用する。
    未設定の場合は~/.cc-memory/ow/orchをデフォルトとして返す。
    いずれもauto-memory管理外ディレクトリに配置する（frontmatter書き換え防止）。
    """
    if OW_QUEUE_DIR:
        return Path(OW_QUEUE_DIR).expanduser()
    return Path.home() / ".cc-memory" / "ow" / "orch"


def _build_queue_frontmatter(
    topic_id: str,
    orch_activity_id: int | None,
    channel_code: str,
    orch_cwd: str,
    last_seen_msg_id: int = 0,
) -> str:
    """queueファイルのYAML frontmatterを生成して返す。"""
    fm_data = {
        "topic_id": int(topic_id) if topic_id.isdigit() else topic_id,
        "orch_activity_id": orch_activity_id,
        "channel_code": channel_code,
        "orch_cwd": orch_cwd,
        "last_seen_msg_id": last_seen_msg_id,
    }
    fm_yaml = yaml.safe_dump(fm_data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{fm_yaml}---\n"


def _sanitize_queue_field(value: str) -> str:
    """queueエントリの1行フィールドに埋め込む値から改行を除去する。

    queueフォーマットは「1フィールド=1行」が不変条件。値に生の改行が含まれると
    行が分割され、`## `で始まる行がファントムタスクとして誤パースされたり、
    _upsert_queue_taskのブロック境界検出（次の`## `行まで）が内部行で誤停止して
    ファイルが破損する。改行（CR/LF）を空白1つに畳んでこれを防ぐ。

    acceptanceやnoteのようなorch自由記述フィールド（複数行が常態）が主な対象。
    """
    return " ".join(str(value).splitlines()).strip()


# MCP/Claudeプロトコルで使われる予約XMLタグのパターン（antml:プレフィックス含む）
_MCP_RESERVED_TAG_RE = re.compile(
    r"</?(?:antml:)?(?:function_calls|invoke|parameter|tool_result)(?:\s[^>]*)?>",
    re.IGNORECASE,
)


def _sanitize_task_body_field(value: str, field_name: str = "") -> str:
    """task_file本文フィールド（acceptance/context等）からMCP予約XMLタグを除去する。

    orchがtool callの引数としてフィールド値を渡すとき、XML構文ミスでタグ残骸
    （例: </parameter>, <invoke name="..."> 等）が混入することがある。
    workerがtask_fileとして読むとき、これらがMCPプロトコルの一部として
    誤解釈される恐れがあるため、既知の予約タグは除去する。
    """
    cleaned, count = _MCP_RESERVED_TAG_RE.subn("", value)
    if count:
        logger.warning(
            "_write_task_file: %sフィールドにMCP予約XMLタグが%d件混入していました（除去済み）",
            field_name or "unknown",
            count,
        )
    return cleaned


def _format_queue_task_entry(
    task_n: int,
    title: str,
    status: str,
    fields: list[tuple[str, str]],
) -> str:
    """正式queueフォーマットのタスクエントリブロックを文字列で返す。

    出力例:
        ## T1 | タスク名 | working
        - worker: w-a / term_ref: iterm2:xxx / session: uuid
        - activity: 801
        - cwd: ~/workspace/cc-memory/.trees/feature-xxx
        - note: 実装中

    末尾に改行を1つ含み、先頭に余分な空行は付けない（空白制御は_upsert_queue_task側の責務）。
    fieldsは (キー, 値) のタプル列。順序はそのまま保持される。
    title・status・各値は_sanitize_queue_fieldで改行を畳んでからフォーマットに埋め込む
    （1フィールド=1行の不変条件を守り、フォーマット破壊・ファントムタスク注入を防ぐ）。
    """
    title = _sanitize_queue_field(title)
    status = _sanitize_queue_field(status)
    lines = [f"## T{task_n} | {title} | {status}"]
    lines.extend(f"- {key}: {_sanitize_queue_field(value)}" for key, value in fields)
    return "\n".join(lines) + "\n"


def _upsert_queue_task(
    queue_dir: Path,
    topic_id: str,
    task_n: int,
    entry_text: str,
    frontmatter: str | None = None,
) -> None:
    """queueファイルのT<n>タスクエントリを追加または置換する（queue状態更新の内部関数）。

    挙動:
    - ファイルが存在しない/空の場合: frontmatter（指定時）＋entry_textで初期化する。
    - 同じT<n>のエントリが既に存在する場合: そのブロックのみを置換する
      （他タスクのエントリやorchが手で編集したnote等はそのまま保持される）。
    - 存在しない場合: ファイル末尾に空行区切りで追記する。

    既存ファイルのfrontmatterには一切触れない（frontmatter更新はorchのEditツール責務）。
    fcntl.flockで排他ロックし、並列spawn時のread-modify-write競合（lost update）を防ぐ。

    MCPツール化はせず、ow_spawn_worker等のow_service内部からのみ呼び出す。
    """
    queue_file = queue_dir / f"queue-t{topic_id}.md"
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    header_prefix = f"## T{task_n} | "

    fd = os.open(str(queue_file), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        content = os.read(fd, size).decode("utf-8") if size else ""

        if not content.strip():
            # 新規 or 空ファイル: frontmatter（あれば）の後に空行を1つ挟んでエントリを置く
            fm = frontmatter or ""
            new_content = f"{fm}\n{entry_text}" if fm else entry_text
        else:
            lines = content.splitlines(keepends=True)
            start = next(
                (i for i, line in enumerate(lines) if line.startswith(header_prefix)),
                None,
            )
            if start is None:
                # 追記: 直前の内容との間に空行を1つ確保する
                if content.endswith("\n\n"):
                    sep = ""
                elif content.endswith("\n"):
                    sep = "\n"
                else:
                    sep = "\n\n"
                new_content = f"{content}{sep}{entry_text}"
            else:
                # 既存T<n>ブロックを置換（次の'## 'ヘッダーまで、なければEOFまで）
                end = len(lines)
                for j in range(start + 1, len(lines)):
                    if lines[j].startswith("## "):
                        end = j
                        break
                before = "".join(lines[:start])
                after = "".join(lines[end:])
                # 後続ブロックがある場合は空行で区切る
                sep = "\n" if (after and not entry_text.endswith("\n\n")) else ""
                new_content = f"{before}{entry_text}{sep}{after}"

        data = new_content.encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, data)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_queue_spawning(
    queue_dir: Path,
    topic_id: str,
    alias: str,
    task_n: int,
    cwd: str,
    task_title: str = "",
    model: str = "",
    acceptance: str = "",
    orch_activity_id: int | None = None,
    channel_code: str = "",
    orch_cwd: str = "",
) -> None:
    """spawning write-aheadを正式queueフォーマットのタスクエントリとして記録する（孤児worker対策）。

    旧実装のWAL風追記（`## T<n> | spawning | spawning`）をやめ、title・model・activity・
    acceptance等を含む正式エントリ（status=spawning）を_upsert_queue_task経由で書き込む。
    こうすることでspawning直後でもorchはqueueファイルから完全なタスク情報を読み取れ、
    再spawn時はエントリが重複追記されず置換される。

    新規ファイル作成時のみYAML frontmatter（topic_id, orch_activity_id, channel_code,
    orch_cwd, last_seen_msg_id）を生成する。既存ファイルのfrontmatterには触れない。
    """
    now = datetime.now(timezone.utc).isoformat()

    fields: list[tuple[str, str]] = [
        ("worker", f"{alias} / term_ref: (pending) / session: (pending)"),
    ]
    if orch_activity_id is not None:
        fields.append(("activity", str(orch_activity_id)))
    if model:
        fields.append(("model", model))
    fields.append(("cwd", cwd))
    fields.append(("spawning", now))
    if acceptance:
        fields.append(("acceptance", acceptance))
    fields.append(("note", "spawning write-ahead"))

    entry_text = _format_queue_task_entry(
        task_n=task_n,
        title=task_title or "(untitled)",
        status="spawning",
        fields=fields,
    )

    frontmatter = _build_queue_frontmatter(
        topic_id=topic_id,
        orch_activity_id=orch_activity_id,
        channel_code=channel_code,
        orch_cwd=orch_cwd,
        last_seen_msg_id=0,
    )

    _upsert_queue_task(
        queue_dir=queue_dir,
        topic_id=topic_id,
        task_n=task_n,
        entry_text=entry_text,
        frontmatter=frontmatter,
    )


def _slugify_task_title(title: str, max_len: int = 40) -> str:
    """task fileのファイル名に使うslugをタイトルから生成する。

    「main — detail」構造のタイトルはmain部分のみを採用し、
    空白・パス上危険な文字（/ \\ | : # ? * < > " ' 改行等）を `-` に畳む。
    日本語はそのまま残す（日本語話者がファイル名から内容を即把握できるようにするため）。
    連続する `-` は1つに畳み、max_len文字で切り詰める。空文字列なら "" を返す。
    """
    if not title:
        return ""
    # 「main — detail」構造ならmain部分のみを使う
    for sep in (" — ", " – ", " - ", "—", "–"):
        if sep in title:
            title = title.split(sep, 1)[0]
            break
    slug = re.sub(r"""[\s/\\|:#?*<>"']+""", "-", title.strip())
    slug = re.sub(r"-+", "-", slug).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug


def _write_task_file(
    task_dir: Path,
    task_n: int,
    alias: str,
    channel: str,
    cwd: str,
    model: str,
    task_title: str,
    acceptance: str,
    context: str,
    playbook: str,
    timeout_min: int,
    activity_id: int | None,
    topic_id: str | None,
) -> Path:
    """task fileをマークダウン（YAML frontmatter + 本文）で書き出す。

    機械可読フィールド（task/alias/channel/cwd/model等）はfrontmatterに、
    人間可読な内容（タイトル・acceptance・context・playbook）は本文に置く。
    workerはfrontmatterから起動パラメータを、本文からタスク内容を読み取る。

    ファイル名は `t<topic_id>-T<n>-<title-slug>.md`。topic prefixでtopic間の名前衝突を、
    title slugで人間がファイルを開かずに内容を把握できることを担保する。
    topic_idが未指定の場合は `T<n>-<title-slug>.md`、slugが空なら接尾辞を省く。
    """
    base = f"t{topic_id}-T{task_n}" if (topic_id is not None and str(topic_id)) else f"T{task_n}"
    slug = _slugify_task_title(task_title)
    name = f"{base}-{slug}" if slug else base
    task_file = task_dir / f"{name}.md"
    task_file.parent.mkdir(parents=True, exist_ok=True)

    fm_data = {
        "v": 1,
        "task": f"T{task_n}",
        "alias": alias,
        "channel": channel,
        "cwd": cwd,
        "model": model,
        "permission_mode": "auto",
        "timeout_min": timeout_min,
        "activity_id": activity_id,
        "topic_id": topic_id,
    }
    fm_yaml = yaml.safe_dump(fm_data, default_flow_style=False, allow_unicode=True, sort_keys=False)

    acceptance_clean = _sanitize_task_body_field(acceptance, "acceptance")
    context_clean = _sanitize_task_body_field(context, "context")
    playbook_clean = _sanitize_task_body_field(playbook, "playbook")

    body_lines = [f"# {fm_data['task']}: {task_title}".rstrip()]
    if acceptance_clean:
        body_lines += ["", "## Acceptance", "", acceptance_clean]
    if context_clean:
        body_lines += ["", "## Context", "", context_clean]
    if playbook_clean:
        body_lines += ["", "## Playbook", "", playbook_clean]
    body = "\n".join(body_lines) + "\n"

    content = f"---\n{fm_yaml}---\n\n{body}"
    task_file.write_text(content, encoding="utf-8")

    return task_file


def _get_adapter_path(terminal: str) -> Path | None:
    """アダプタスクリプトのパスを返す（不在ならNone）。"""
    scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts" / "ow" / "adapters"
    adapter = scripts_dir / f"{terminal}.sh"
    if adapter.exists():
        return adapter
    return None


def ow_spawn_worker(
    alias: str,
    channel: str,
    cwd: str,
    model: str,
    task_title: str = "",
    acceptance: str = "",
    context: str = "",
    playbook: str = "",
    timeout_min: int = 60,
    activity_id: int | None = None,
    topic_id: str | None = None,
    task_n: int = 1,
    tmux_target_pane: str | None = None,
) -> dict:
    """workerセッションを起動する。

    処理順: spawn前ヘルスチェック → queueへspawning write-ahead → task file書き出し
        → アダプタ呼び出し → 安定ID返却

    permission_modeは常にautoに固定される。

    Args:
        alias: workerのhandle（例: "w-a"）
        channel: channelコード
        cwd: workerの作業ディレクトリ
        model: 使用モデル（例: "sonnet", "opus"）
        task_title: タスクタイトル
        acceptance: 完了条件
        context: タスクコンテキスト
        playbook: プレイブック抜粋
        timeout_min: タイムアウト（分）
        activity_id: 対応するアクティビティID
        topic_id: 対応するトピックID
        task_n: タスク番号（Tn）
        tmux_target_pane: OW_TERMINAL=tmuxのとき、worker paneを分割する基準pane ID。
            指定時はそのpaneと同じwindow内に split-window で入れる（最初は右に30%水平、
            以降は最新worker paneを垂直分割）。未指定時は従来の `ow-workers` 別sessionに
            新windowで起動する。クライアント（spawn呼び出し元）が自身の os.environ['TMUX_PANE']
            を読んで渡す想定。MCPサーバープロセスのenvは起動時にフリーズするためサーバー側で
            参照できない。

    Returns:
        {"term_ref": str, "task_file": str, "spawning": "ok"}
        manualフォールバック時: {"command": str, "manual": True, "task_file": str}
        spawn前検証失敗時: {"error": {"code": "SPAWN_PRECONDITION_FAILED", "warnings": [...]}}
    """
    # model validation / normalization
    model, model_error = _normalize_and_validate_model(model)
    if model_error:
        return {
            "error": {
                "code": "INVALID_MODEL",
                "message": model_error,
            },
        }

    # spawn前ヘルスチェック (relay疎通・channel存在・cwd存在・alias重複)
    preflight = _validate_spawn_preconditions(alias, channel, cwd, topic_id=topic_id, task_n=task_n)
    if not preflight["ok"]:
        return {
            "error": {
                "code": "SPAWN_PRECONDITION_FAILED",
                "message": "spawn precondition check failed",
                "warnings": preflight["warnings"],
            },
        }

    queue_dir = _get_queue_dir()
    task_dir = queue_dir / "tasks"

    # queueへspawning write-ahead（孤児worker対策）
    orch_cwd = os.environ.get("OW_ORCH_CWD", "")
    if not orch_cwd:
        orch_cwd = os.getcwd()
        logger.warning(
            "OW_ORCH_CWD not set, using cwd=%s as orch_cwd. "
            "Crash recovery requires the same cwd.",
            orch_cwd,
        )
    if topic_id is not None:
        _write_queue_spawning(
            queue_dir,
            str(topic_id),
            alias,
            task_n,
            cwd,
            task_title=task_title,
            model=model,
            acceptance=acceptance,
            orch_activity_id=activity_id,
            channel_code=channel,
            orch_cwd=orch_cwd,
        )

    # task fileを書き出す
    task_file = _write_task_file(
        task_dir=task_dir,
        task_n=task_n,
        alias=alias,
        channel=channel,
        cwd=cwd,
        model=model,
        task_title=task_title,
        acceptance=acceptance,
        context=context,
        playbook=playbook,
        timeout_min=timeout_min,
        activity_id=activity_id,
        topic_id=topic_id,
    )

    # アダプタ起動
    terminal = os.environ.get("OW_TERMINAL", "manual")
    adapter_path = _get_adapter_path(terminal) if terminal != "manual" else None

    # --add-dir は variadic option (`<directories...>`) のため、続く positional prompt まで
    # ディレクトリ引数として食ってしまう。`--` で variadic を打ち切って prompt を positional
    # として確実に届ける。
    worker_cmd = (
        f'env OW_ROLE=worker OW_ALIAS={shlex.quote(alias)} OW_CHANNEL={shlex.quote(channel)} '
        f'OW_TASK_FILE={shlex.quote(str(task_file))} '
        f'claude --model {shlex.quote(model)} --permission-mode auto '
        f'--add-dir {shlex.quote(str(task_file.parent))} -- '
        f'{shlex.quote(f"workerスキルに従って作業を開始して。task: {task_file}")}'
    )

    if adapter_path is None:
        # manualフォールバック: 起動コマンドを返す
        return {
            "command": worker_cmd,
            "manual": True,
            "task_file": str(task_file),
            "alias": alias,
        }

    # アダプタ呼び出し — stdoutから安定IDを取得する
    adapter_args = ["bash", str(adapter_path), "spawn", cwd, worker_cmd]
    if terminal == "tmux" and tmux_target_pane:
        adapter_args.append(tmux_target_pane)
    try:
        result = subprocess.run(
            adapter_args,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        term_ref = result.stdout.strip()
        if not term_ref:
            term_ref = str(uuid.uuid4())
            logger.warning("adapter returned empty term_ref, using fallback UUID: %s", term_ref)
    except subprocess.TimeoutExpired:
        logger.warning("adapter spawn timed out after 30s")
        return {
            "command": worker_cmd,
            "manual": True,
            "task_file": str(task_file),
            "alias": alias,
            "adapter_error": "adapter spawn timed out",
        }
    except subprocess.CalledProcessError as e:
        logger.warning("adapter spawn failed: %s", e.stderr)
        return {
            "command": worker_cmd,
            "manual": True,
            "task_file": str(task_file),
            "alias": alias,
            "adapter_error": e.stderr,
        }

    return {
        "term_ref": term_ref,
        "task_file": str(task_file),
        "spawning": "ok",
        "alias": alias,
    }


def ow_close_worker(term_ref: str) -> dict:
    """アダプタ経由でworkerセッションをクローズする。

    Args:
        term_ref: 安定ID（iterm2のsession UUID、tmuxのpane ID等）

    Returns:
        {"closed": True} または {"error": ...}
    """
    terminal = os.environ.get("OW_TERMINAL", "manual")
    adapter_path = _get_adapter_path(terminal) if terminal != "manual" else None

    if adapter_path is None:
        return {
            "manual": True,
            "message": f"手動でterm_ref={term_ref}のセッションをクローズしてください",
        }

    try:
        subprocess.run(
            ["bash", str(adapter_path), "close", term_ref],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return {"closed": True, "term_ref": term_ref}
    except subprocess.TimeoutExpired:
        logger.warning("adapter close timed out after 15s")
        return {
            "error": {"code": "ADAPTER_CLOSE_TIMEOUT", "message": "adapter close timed out"},
            "term_ref": term_ref,
        }
    except subprocess.CalledProcessError as e:
        logger.warning("adapter close failed: %s", e.stderr)
        return {
            "error": {"code": "ADAPTER_CLOSE_FAILED", "message": e.stderr},
            "term_ref": term_ref,
        }


# ----------------------------
# ow_status
# ----------------------------


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """YAMLフロントマターをパースして(frontmatter_dict, rest_content)を返す。

    frontmatterが存在しない場合は ({}, content) を返す。
    YAMLパースエラー時は ({}, rest_content) にフォールバックする（本文のタスクパースは継続）。
    """
    if not content.startswith("---"):
        return {}, content

    # 2番目の '---' を探す
    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return {}, content

    fm_text = content[3:end_idx].strip()
    rest = content[end_idx + 4:]  # '\n---'（4文字）の後

    try:
        fm_data = yaml.safe_load(fm_text)
        if not isinstance(fm_data, dict):
            return {}, rest
        return fm_data, rest
    except yaml.YAMLError:
        return {}, rest


def _parse_queue_file(queue_file: Path) -> tuple[dict, list[dict]]:
    """queueファイルをパースして (frontmatter, tasks) のタプルを返す。

    Returns:
        (frontmatter_dict, tasks_list)
        - frontmatter_dict: YAMLフロントマターのdict。存在しない場合は {}
        - tasks_list: タスク一覧のリスト

    後方互換: frontmatterなしのqueueファイルは ({}, tasks) を返す。
    YAMLパースエラー時は ({}, tasks) にフォールバックし、タスク部分は正常パースする。
    空ファイル・存在しないファイルは ({}, []) を返す。
    """
    if not queue_file.exists():
        return {}, []

    try:
        content = queue_file.read_text(encoding="utf-8")
    except OSError:
        return {}, []

    if not content.strip():
        return {}, []

    # frontmatterをパース
    frontmatter, body = _parse_frontmatter(content)

    tasks = []
    current_task: dict | None = None

    for line in body.splitlines():
        line = line.strip()
        # タスクヘッダー: ## T1 | タイトル | status
        if line.startswith("## T") and " | " in line:
            if current_task is not None:
                tasks.append(current_task)
            parts = line.lstrip("# ").split(" | ")
            task_id = parts[0].strip() if len(parts) > 0 else "?"
            task_status = parts[-1].strip() if len(parts) > 2 else "unknown"
            task_title = " | ".join(parts[1:-1]).strip() if len(parts) > 2 else (parts[1].strip() if len(parts) > 1 else "")
            current_task = {
                "task": task_id,
                "title": task_title,
                "status": task_status,
                "worker": None,
                "term_ref": None,
            }
        elif current_task is not None and line.startswith("- worker:"):
            # "- worker: w-a / term_ref: iterm2:xxx / session: <uuid>"
            worker_info = line[len("- worker:"):].strip()
            for part in worker_info.split("/"):
                part = part.strip()
                if part.startswith("term_ref:"):
                    current_task["term_ref"] = part[len("term_ref:"):].strip()
                elif not part.startswith("session:"):
                    current_task["worker"] = part

    if current_task is not None:
        tasks.append(current_task)

    return frontmatter, tasks


def ow_status(channel: str, topic_id: str | None = None) -> dict:
    """queueサマリ＋GetPresence（worker死活）の合成ビュー。

    Args:
        channel: channelコード（presence取得に使用）
        topic_id: queueファイル特定に使用（OW_QUEUE_DIRと組み合わせ）

    Returns:
        {
            "tasks": [...],
            "presence": [...],
            "frontmatter": {...},  # queueのfrontmatter情報（channel_code, last_seen_msg_id等）
            "summary": {"total_tasks": int, "status_counts": dict, "online_workers": [...]}
        }
    """
    # relayサーバー確認 → 未起動なら自動起動
    if not ensure_relay_server():
        return {"error": {"code": "RELAY_UNAVAILABLE", "message": "relay server is not available"}}

    # channel指定があればensure_channel（idempotent）
    if channel:
        if not ensure_channel(channel):
            return {"error": {"code": "CHANNEL_UNAVAILABLE", "message": f"channel {channel} could not be created"}}

    # presenceを取得
    presence_result = _relay_request("GET", f"/presence?{urllib.parse.urlencode({'channel': channel})}")
    if "error" in presence_result:
        handles = []
    else:
        handles = presence_result.get("handles", [])

    # queueファイルをパース
    tasks: list[dict] = []
    frontmatter: dict = {}
    queue_dir = _get_queue_dir()
    if topic_id is not None:
        queue_file = queue_dir / f"queue-t{topic_id}.md"
        frontmatter, tasks = _parse_queue_file(queue_file)
    else:
        # topic_id未指定の場合は存在する全queueファイルを読む
        if queue_dir.exists():
            for queue_file in sorted(queue_dir.glob("queue-t*.md")):
                fm, file_tasks = _parse_queue_file(queue_file)
                tasks.extend(file_tasks)
                if fm and not frontmatter:
                    # 1 orch = 1 topic のため topic_id 指定が原則。
                    # topic_id未指定の全件走査は診断用途のみ想定し、最初のfrontmatterを代表とする。
                    frontmatter = fm

    # presenceとqueueの統合
    for task in tasks:
        worker = task.get("worker")
        if worker:
            task["online"] = worker in handles

    # サマリ
    status_counts: dict[str, int] = {}
    for task in tasks:
        s = task.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    return {
        "tasks": tasks,
        "presence": handles,
        "frontmatter": frontmatter,
        "summary": {
            "total_tasks": len(tasks),
            "status_counts": status_counts,
            "online_workers": [h for h in handles if h.startswith("w-")],
        },
    }


# ----------------------------
# crash復旧自動化・spawn前バリデーション
# ----------------------------
#
# 設計方針:
#   - 突合ロジックの中核は relay messages history + presence のみで成立し、queue層の物理形に依存しない
#     (queue層が活動activity/log/material化される可能性に対する将来耐性)
#   - queue層との接点は `_apply_queue_status_update` 1点に集約。queue層が変わったらこの関数だけ書き換えれば済む
#   - reconstruct_state_from_relay と detect_crash_inconsistencies は純粋関数として実装し、テスト容易性を確保
#
# 突合分類:
#   - ghost_active: queue=spawning/assigned/working かつ presence offline → relay最新stateから状態再構築
#   - stalled_done: queue=done/closed/cancelled/failed かつ presence online → ping送信で素性照会
#   - orphans:    queue外でpresence onlineのworker handle → ping送信で再リンク照会


def _get_presence(channel: str) -> list[str]:
    """relayのGET /presenceから接続中handle一覧を取得する。

    エラー時は空リストを返し、呼び出し元が「presence情報なし」として扱える設計。
    例外を伝播させない（fail-soft）のは ensure_channel と同じ規律。
    """
    try:
        result = _relay_request("GET", f"/presence?{urllib.parse.urlencode({'channel': channel})}")
    except Exception as e:
        logger.warning("get presence failed for %s: %s", channel, e)
        return []
    if "error" in result:
        return []
    handles = result.get("handles") or []
    return list(handles) if isinstance(handles, list) else []


# queueにおける「workerが活動中」と分類する状態。spawningは含めない（C1対応）:
# spawning 状態は ow_spawn_worker 進行中（terminal adapter 起動待ち、manual起動の人間操作待ち、
# worker起動直後でreadyメッセージ未送信、等）の状態が混在しており、ow_recover がここに介入すると
# 進行中のworker起動とレース条件を起こす。spawning は detect_crash_inconsistencies で別カテゴリ
# pending_spawn として扱う。
_ACTIVE_QUEUE_STATUSES: set[str] = {"assigned", "ready", "working"}
_TERMINAL_QUEUE_STATUSES: set[str] = {"done", "closed", "cancelled", "failed"}

# relay最新state宣言 → queue statusへの再構築マップ。
# presence offline & queue active のghost_activeケースで使う。
# 終端state (done/closed/failed/cancelled) はそのまま反映、非終端 (ready/working等) は
# presence offline = 異常終了とみなして "stalled" に倒す（手動介入を促す）。
# escalated/fallback は本来presence onlineのまま継続する状態だが、offlineで残る場合は
# 人間介入のセッションごと落ちた異常とみなして stalled に倒す（workerスキルの状態モデル参照）。
_RELAY_STATE_TO_QUEUE_STATUS: dict[str, str] = {
    "done": "done",
    "closed": "closed",
    "failed": "failed",
    "cancelled": "cancelled",
    "ready": "stalled",
    "working": "stalled",
    "blocked": "stalled",
    "escalated": "stalled",
    "fallback": "stalled",
    "dead": "failed",
}


def _validate_spawn_preconditions(
    alias: str,
    channel: str,
    cwd: str,
    topic_id: str | None = None,
    task_n: int | None = None,
) -> dict:
    """ow_spawn_worker起動前の一括ヘルスチェック。

    検証項目（すべて満たす必要があり、いずれか失敗すれば ok=False）:
        - relayサーバー疎通: ensure_relay_serverで自己修復込み
        - channel存在: ensure_channelで自動作成
        - cwd存在: Path(cwd).expanduser()がディレクトリとして存在するか
        - alias重複: 同aliasがpresence onlineまたはqueue上で活動中タスクのworkerとして他の
          task_nに割当て済みでないか（同一task_nで再spawn=再リンクは許可）

    Returns:
        {
            "ok": bool,
            "warnings": [str],  # 失敗した検証項目のメッセージ一覧
        }

    呼び出し元はok=Falseならspawnを中止し、warningsをユーザー/orchに見せる責務を持つ。
    """
    warnings: list[str] = []

    # 1. relay疎通
    if not ensure_relay_server():
        warnings.append("relay server unreachable (ensure_relay_server returned False)")
        # 以降のチェックはrelay前提のため、ここで早期return
        return {"ok": False, "warnings": warnings}

    # 2. channel存在
    if not ensure_channel(channel):
        warnings.append(f"channel {channel} unavailable (ensure_channel returned False)")

    # 3. cwd存在
    try:
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.is_dir():
            warnings.append(f"cwd does not exist or is not a directory: {cwd}")
    except (OSError, ValueError) as e:
        warnings.append(f"cwd validation error for {cwd}: {e}")

    # 4. alias重複 (presence)
    presence = _get_presence(channel)
    if alias in presence:
        warnings.append(
            f"alias {alias} is already present (online) in channel {channel}"
        )

    # 5. alias重複 (queue active task)
    if topic_id is not None:
        queue_dir = _get_queue_dir()
        queue_file = queue_dir / f"queue-t{topic_id}.md"
        _, tasks = _parse_queue_file(queue_file)
        for t in tasks:
            t_status = t.get("status", "")
            t_worker = t.get("worker")
            t_task = t.get("task", "")
            if t_worker != alias or t_status not in _ACTIVE_QUEUE_STATUSES:
                continue
            # 同一task_n（"T<n>"）に対する再spawnは再リンクとみなして許可
            if task_n is not None and t_task == f"T{task_n}":
                continue
            warnings.append(
                f"alias {alias} already has active queue task {t_task} (status={t_status})"
            )
            break

    return {"ok": not warnings, "warnings": warnings}


def reconstruct_state_from_relay(channel: str, limit: int = 10000) -> dict:
    """relay履歴をsince=0で全件pullし、(alias, task) 単位の最新state宣言を集計する。

    queue層の物理形に依存しない純粋関数。queueがcc-memory activityに移行しても、
    queueファイルが残っても、このアルゴリズムは流用可能。

    Args:
        channel: channelコード
        limit: 1回のpull上限（msg数）。10000は通常topic数百タスク分を十分にカバーする。
            これを超える長期topicでは将来的にwindowed pullが必要だが、現状は単純実装でよい。

    Returns:
        成功時:
            {
                "by_worker_task": {
                    "<alias>:T<n>": {
                        "alias": str,
                        "task": str,        # "T<n>"
                        "latest_state": str,
                        "latest_msg_id": int,
                        "latest_at": str,
                        "history_count": int,
                    },
                    ...
                },
                "max_msg_id": int,
            }
        失敗時:
            {"by_worker_task": {}, "max_msg_id": 0, "error": {...}}
    """
    history = ow_history(channel, since=0, limit=limit)
    if "error" in history:
        return {"by_worker_task": {}, "max_msg_id": 0, "error": history["error"]}

    messages = history.get("messages", [])
    truncated = len(messages) >= limit
    by_worker_task: dict[str, dict] = {}
    max_msg_id = 0

    for msg in messages:
        msg_id = msg.get("msg_id", 0)
        if isinstance(msg_id, int) and msg_id > max_msg_id:
            max_msg_id = msg_id
        body = msg.get("body", {})
        if not isinstance(body, dict):
            continue
        if body.get("kind") != "event":
            continue
        data = body.get("data") or {}
        if data.get("type") != "state":
            continue
        alias = body.get("from") or ""
        task = body.get("task") or ""
        state = data.get("state") or ""
        if not alias or not task or not state:
            continue
        key = f"{alias}:{task}"
        entry = by_worker_task.setdefault(
            key,
            {
                "alias": alias,
                "task": task,
                "latest_state": state,
                "latest_msg_id": msg_id,
                "latest_at": msg.get("created_at", ""),
                "history_count": 0,
            },
        )
        entry["history_count"] += 1
        # msg_id順序保証はrelay側に頼らず、エントリ側でmaxを取る
        if msg_id >= entry["latest_msg_id"]:
            entry["latest_state"] = state
            entry["latest_msg_id"] = msg_id
            entry["latest_at"] = msg.get("created_at", "")

    return {"by_worker_task": by_worker_task, "max_msg_id": max_msg_id, "truncated": truncated}


def detect_crash_inconsistencies(
    queue_tasks: list[dict],
    reconstructed: dict,
    presence: list[str],
) -> dict:
    """queue状態 × relay最新state × presence の3つを突合し、不整合を4カテゴリに分類する。

    純粋関数（I/Oなし）。テスト容易性のためqueue/relay/presenceは全て引数で受け取る。

    Args:
        queue_tasks: `_parse_queue_file` の返り値相当
            （[{task, title, status, worker, term_ref}, ...]）
        reconstructed: `reconstruct_state_from_relay` の返り値
        presence: `_get_presence` の返り値

    Returns:
        {
            "ghost_active": [    # queue活動中(assigned/ready/working)だがpresence offline
                {task, alias, queue_status, latest_state, latest_msg_id, latest_at, suggested_status},
                ...
            ],
            "pending_spawn": [   # queue=spawning。relay履歴の有無で2通りに分かれる
                {task, alias, queue_status, has_relay_history, latest_state, latest_msg_id, latest_at, suggested_status},
                ...
            ],
            "stalled_done": [    # queue終端だがworkerがpresenceに残存
                {task, alias, queue_status},
                ...
            ],
            "orphans": [         # presence onlineだがqueue外
                {alias, relay_tasks: [{task, latest_state, latest_msg_id}, ...]},
                ...
            ],
        }

    pending_spawn の解釈（C1対応）:
        - has_relay_history=True: workerが起動してstate宣言を送ったがpresence offline → ghost_active相当の
            扱いで `suggested_status` を入れる（ow_recoverは自動更新する）
        - has_relay_history=False: workerはまだ何も送っていない＝起動進行中の可能性が高い。
            ow_recoverは自動更新せず、orchに「pending_spawn 残留」を伝えるだけにする。
    """
    by_wt = reconstructed.get("by_worker_task", {}) or {}
    presence_set = set(presence or [])

    ghost_active: list[dict] = []
    pending_spawn: list[dict] = []
    stalled_done: list[dict] = []
    queue_worker_aliases: set[str] = set()

    for task in queue_tasks:
        worker = task.get("worker")
        status = task.get("status", "") or ""
        task_id = task.get("task", "") or ""
        if worker:
            queue_worker_aliases.add(worker)

        relay_entry = by_wt.get(f"{worker}:{task_id}") if worker else None

        if status == "spawning" and worker and worker not in presence_set:
            has_history = relay_entry is not None
            latest_state = relay_entry["latest_state"] if relay_entry else None
            suggested = (
                _RELAY_STATE_TO_QUEUE_STATUS.get(latest_state, "stalled")
                if has_history
                else None  # 起動進行中の可能性 → ow_recoverは触らない
            )
            pending_spawn.append(
                {
                    "task": task_id,
                    "alias": worker,
                    "queue_status": status,
                    "has_relay_history": has_history,
                    "latest_state": latest_state,
                    "latest_msg_id": relay_entry["latest_msg_id"] if relay_entry else 0,
                    "latest_at": relay_entry["latest_at"] if relay_entry else "",
                    "suggested_status": suggested,
                }
            )
        elif status in _ACTIVE_QUEUE_STATUSES and worker and worker not in presence_set:
            latest_state = relay_entry["latest_state"] if relay_entry else None
            suggested = (
                _RELAY_STATE_TO_QUEUE_STATUS.get(latest_state, "stalled")
                if latest_state
                else "stalled"
            )
            ghost_active.append(
                {
                    "task": task_id,
                    "alias": worker,
                    "queue_status": status,
                    "latest_state": latest_state,
                    "latest_msg_id": relay_entry["latest_msg_id"] if relay_entry else 0,
                    "latest_at": relay_entry["latest_at"] if relay_entry else "",
                    "suggested_status": suggested,
                }
            )
        elif status in _TERMINAL_QUEUE_STATUSES and worker and worker in presence_set:
            stalled_done.append(
                {
                    "task": task_id,
                    "alias": worker,
                    "queue_status": status,
                }
            )

    orphans: list[dict] = []
    for handle in sorted(presence_set):
        if not handle.startswith("w-"):
            continue
        if handle in queue_worker_aliases:
            continue
        relay_tasks = [
            {
                "task": v["task"],
                "latest_state": v["latest_state"],
                "latest_msg_id": v["latest_msg_id"],
            }
            for v in by_wt.values()
            if v["alias"] == handle
        ]
        relay_tasks.sort(key=lambda x: x["latest_msg_id"])
        orphans.append({"alias": handle, "relay_tasks": relay_tasks})

    return {
        "ghost_active": ghost_active,
        "pending_spawn": pending_spawn,
        "stalled_done": stalled_done,
        "orphans": orphans,
    }


def _send_recovery_ping(channel: str, alias: str, task: str = "T0") -> dict:
    """workerにcrash復旧用pingを送信する（kind:command / data.type:ping）。

    needs_reply=True で送り、応答はorchの通常受信ループで処理される。
    本関数はfire-and-forget（応答待ちはしない）。
    """
    body = {
        "v": 1,
        "kind": "command",
        "from": "orch",
        "to": alias,
        "task": task or "T0",
        "data": {"type": "ping", "recovery": True},
    }
    return ow_send(channel=channel, handle="orch", body=body, needs_reply=True)


def _apply_queue_status_update(
    queue_dir: Path,
    topic_id: str,
    task: str,
    new_status: str,
    note: str = "",
) -> None:
    """queueファイルの指定タスクのstatusヘッダーのみを更新する（他フィールドは保持）。

    queue層との接点はこの関数1箇所に集約してある。queue層がcc-memory entityに
    置換される場合も、ここを書き換えれば突合ロジック (`detect_crash_inconsistencies`) は無改修で済む。

    挙動:
        - `## T<n> | <title> | <status>` ヘッダーのstatus部分のみ置換
        - noteが指定されていれば既存の `- note:` 行を置換、なければブロック末尾に追加
        - flockでwrite競合を防ぐ（`_upsert_queue_task` と同じ規律）
    """
    queue_file = queue_dir / f"queue-t{topic_id}.md"
    if not queue_file.exists():
        raise FileNotFoundError(f"queue file not found: {queue_file}")

    if not task.startswith("T"):
        raise ValueError(f"unexpected task id format: {task}")

    header_prefix = f"## {task} | "

    fd = os.open(str(queue_file), os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        content = os.read(fd, size).decode("utf-8") if size else ""
        lines = content.splitlines(keepends=True)
        start = next(
            (i for i, line in enumerate(lines) if line.startswith(header_prefix)),
            None,
        )
        if start is None:
            raise KeyError(f"task {task} header not found in {queue_file}")

        # ヘッダー行のstatus（最終 ' | ' フィールド）を置換
        original = lines[start].rstrip("\n")
        parts = original.split(" | ")
        if len(parts) >= 3:
            parts[-1] = new_status
            new_header = " | ".join(parts) + "\n"
        else:
            # フォーマット崩れフォールバック: 強制再構築
            new_header = f"## {task} | (recovered) | {new_status}\n"
        lines[start] = new_header

        # 当該タスクブロックの範囲: start+1 〜 次の '## ' まで
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break

        if note:
            note_idx = next(
                (i for i in range(start + 1, end) if lines[i].startswith("- note:")),
                None,
            )
            new_note_line = f"- note: {_sanitize_queue_field(note)}\n"
            if note_idx is not None:
                lines[note_idx] = new_note_line
            else:
                insert_at = end
                while insert_at > start + 1 and lines[insert_at - 1].strip() == "":
                    insert_at -= 1
                lines.insert(insert_at, new_note_line)

        data = "".join(lines).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, data)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def ow_recover(channel: str, topic_id: str, dry_run: bool = False) -> dict:
    """orch crash後のqueue × relay履歴 × presence整合チェック・自動修正のエントリポイント。

    手順:
        1. ensure_relay_server / ensure_channel で前提整える
        2. reconstruct_state_from_relay で全relay履歴から状態再構築
        3. _get_presence で現在onlineのworker一覧取得
        4. detect_crash_inconsistencies で4カテゴリ（ghost_active/pending_spawn/stalled_done/orphans）に分類
        5. dry_run=Falseなら:
           - ghost_active + pending_spawn(relay履歴あり): queueをrelay最新stateで自動更新
           - stalled_done / orphans: cmd:ping送信（応答待ちはしない）

    orchはこの戻り値を見て、queueの状態が更新されたことを確認し、ping送信先からの応答を
    通常の受信ループで処理する。

    Args:
        channel: channelコード
        topic_id: トピックID（queueファイル特定用、文字列も整数も受け付ける）
        dry_run: Trueなら検出のみ・修正/送信なし

    Returns:
        {
            "detected": {
                ghost_active: [...],
                pending_spawn: [...],
                stalled_done: [...],
                orphans: [...],
            },
            "applied": {queue_updates: [...], pings_sent: [...]},
            "warnings": [str],
            "presence": [str],
            "reconstructed_max_msg_id": int,
            "dry_run": bool,
        }
        relay/channel不可時: {"error": {"code": ..., "message": ...}}
        relay history取得失敗時: detected全カテゴリ空 + warnings に fetch error メッセージ
    """
    warnings: list[str] = []

    if not ensure_relay_server():
        return {
            "error": {"code": "RELAY_UNAVAILABLE", "message": "relay server is not available"}
        }
    if not ensure_channel(channel):
        return {
            "error": {
                "code": "CHANNEL_UNAVAILABLE",
                "message": f"channel {channel} could not be created",
            }
        }

    queue_dir = _get_queue_dir()
    topic_id_str = str(topic_id)
    queue_file = queue_dir / f"queue-t{topic_id_str}.md"
    _, queue_tasks = _parse_queue_file(queue_file)

    reconstructed = reconstruct_state_from_relay(channel)
    if "error" in reconstructed:
        return {
            "detected": {
                "ghost_active": [],
                "pending_spawn": [],
                "stalled_done": [],
                "orphans": [],
            },
            "applied": {"queue_updates": [], "pings_sent": []},
            "warnings": [f"relay history fetch error: {reconstructed['error']}"],
            "presence": [],
            "reconstructed_max_msg_id": 0,
            "dry_run": dry_run,
        }
    if reconstructed.get("truncated"):
        warnings.append(
            "relay history truncated at limit=10000: oldest state declarations may be missing"
        )

    presence = _get_presence(channel)
    detected = detect_crash_inconsistencies(queue_tasks, reconstructed, presence)

    applied: dict = {"queue_updates": [], "pings_sent": []}

    if not dry_run:
        # ghost_active + pending_spawn(has_relay_history=True) を自動更新対象とする。
        # spawning だが relay履歴ゼロ (起動進行中の可能性) は触らない。
        auto_update_targets = list(detected["ghost_active"]) + [
            p for p in detected["pending_spawn"] if p["has_relay_history"]
        ]
        for ghost in auto_update_targets:
            try:
                _apply_queue_status_update(
                    queue_dir=queue_dir,
                    topic_id=topic_id_str,
                    task=ghost["task"],
                    new_status=ghost["suggested_status"],
                    note=(
                        f"crash-recovery: relay最新state={ghost['latest_state']} "
                        f"(msg_id={ghost['latest_msg_id']})で再構築"
                    ),
                )
                applied["queue_updates"].append(
                    {
                        "task": ghost["task"],
                        "alias": ghost["alias"],
                        "from": ghost["queue_status"],
                        "to": ghost["suggested_status"],
                    }
                )
            except (FileNotFoundError, KeyError, ValueError, OSError) as e:
                warnings.append(
                    f"failed to update queue for {ghost['task']} ({ghost['alias']}): {e}"
                )

        for orphan in detected["orphans"]:
            result = _send_recovery_ping(channel, orphan["alias"])
            applied["pings_sent"].append(
                {
                    "alias": orphan["alias"],
                    "task": "T0",
                    "reason": "orphan",
                    "result": result,
                }
            )
        for stalled in detected["stalled_done"]:
            result = _send_recovery_ping(channel, stalled["alias"], task=stalled["task"])
            applied["pings_sent"].append(
                {
                    "alias": stalled["alias"],
                    "task": stalled["task"],
                    "reason": "stalled_done",
                    "result": result,
                }
            )

    return {
        "detected": detected,
        "applied": applied,
        "warnings": warnings,
        "presence": presence,
        "reconstructed_max_msg_id": reconstructed.get("max_msg_id", 0),
        "dry_run": dry_run,
    }


# ----------------------------
# reducer: v3 event sourcing
# ----------------------------


def _parse_ow_event(msg: dict) -> dict | None:
    """relayメッセージからow envelopeを解釈する。

    Args:
        msg: ow_historyから返されるメッセージdict

    Returns:
        {"msg_id": int, "handle": str, "body": dict, "created_at": str} または None
    """
    body = msg.get("body")
    if not isinstance(body, dict):
        return None
    msg_id = msg.get("msg_id")
    v = body.get("v")
    if v != 1:
        logger.warning(
            "ow_service: skip msg_id=%s (envelope v=%r, expected 1)", msg_id, v
        )
        return None
    kind = body.get("kind")
    if kind not in ("command", "event"):
        logger.warning(
            "ow_service: skip msg_id=%s (kind=%r, expected command/event)", msg_id, kind
        )
        return None
    return {
        "msg_id": msg_id,
        "handle": msg.get("handle"),
        "body": body,
        "created_at": msg.get("created_at"),
    }


def _query_latest_event(
    channel: str, handle: str | None, data_type: str, since: int = 0
) -> dict | None:
    """指定 channel/handle/data_type の最新 event を返す（kind=event のみ対象）。

    Args:
        channel: channelコード
        handle: workerハンドル（Noneなら全handle対象）
        data_type: eventのdata.type（例: "identity", "state", "heartbeat"）
        since: このmsg_idより大きいものを返す（0=全件）

    Returns:
        最新のparsed event dict または None
    """
    history = ow_history(channel, since=since, limit=10000)
    if "error" in history:
        return None
    result = None
    for msg in history.get("messages", []):
        if handle is not None and msg.get("handle") != handle:
            continue
        parsed = _parse_ow_event(msg)
        if parsed is None:
            continue
        if parsed["body"].get("kind") != "event":
            continue
        if parsed["body"].get("data", {}).get("type") != data_type:
            continue
        if result is None or parsed["msg_id"] > result["msg_id"]:
            result = parsed
    return result


def _query_events_since(
    channel: str,
    handle: str | None,
    data_type: str | None,
    since: int = 0,
) -> list[dict]:
    """指定条件のeventを時系列順で全件取得（kind=event のみ）。

    Args:
        channel: channelコード
        handle: workerハンドル（Noneなら全handle対象）
        data_type: eventのdata.type（Noneなら全type）
        since: このmsg_idより大きいものを返す（0=全件）

    Returns:
        条件に合うparsed event dictのリスト（msg_id昇順）
    """
    history = ow_history(channel, since=since, limit=10000)
    if "error" in history:
        return []
    results = []
    for msg in history.get("messages", []):
        if handle is not None and msg.get("handle") != handle:
            continue
        parsed = _parse_ow_event(msg)
        if parsed is None:
            continue
        if parsed["body"].get("kind") != "event":
            continue
        if data_type is not None and parsed["body"].get("data", {}).get("type") != data_type:
            continue
        results.append(parsed)
    return results


def _infer_crash_cause(
    workload_state: str | None, last_heartbeat_at: str | None
) -> str | None:
    """crash推論ロジック（DB不変、戻り値のみに付与）。

    Args:
        workload_state: 最新のworkload state文字列
        last_heartbeat_at: 最後のheartbeat受信時刻（ISO形式文字列）

    Returns:
        crash推論結果文字列 または None（crashでない場合）

    Notes:
        escalated は workload state machine 上は non-terminal だが、人間対話中の
        worker は heartbeat 停止が「異常」を意味しないため watchdog 対象外
        （orch SKILL.md・playbook.md でも明示）。crash 推論からも除外する。
    """
    if workload_state not in _NON_TERMINAL_WORKLOAD_STATES:
        return None
    if workload_state == "escalated":
        return None
    if last_heartbeat_at is None:
        return None
    try:
        hb_time = datetime.fromisoformat(last_heartbeat_at)
        if hb_time.tzinfo is None:
            hb_time = hb_time.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - hb_time).total_seconds()
        timeout = _HEARTBEAT_TIMEOUT_SECS.get(workload_state, _HEARTBEAT_TIMEOUT_DEFAULT)
        if elapsed < timeout:
            return None
        if workload_state == "draining":
            return "crashed-during-drain (inferred)"
        return "crashed (inferred)"
    except (ValueError, TypeError):
        return None


def _latest_events_by_type(
    channel: str, handle: str | None, data_types: tuple[str, ...]
) -> dict[str, dict]:
    """指定 handle の各 data_type について最新 event を1回の ow_history で取得する。

    Args:
        channel: channelコード
        handle: workerハンドル（Noneなら全handle対象）
        data_types: 対象とする event の data.type タプル

    Returns:
        {data_type: latest_event_dict} — 該当 event が無い type はキー欠落
    """
    history = ow_history(channel, since=0, limit=10000)
    if "error" in history:
        return {}
    latest: dict[str, dict] = {}
    for msg in history.get("messages", []):
        if handle is not None and msg.get("handle") != handle:
            continue
        parsed = _parse_ow_event(msg)
        if parsed is None:
            continue
        if parsed["body"].get("kind") != "event":
            continue
        t = parsed["body"].get("data", {}).get("type")
        if t not in data_types:
            continue
        current = latest.get(t)
        if current is None or parsed["msg_id"] > current["msg_id"]:
            latest[t] = parsed
    return latest


def ow_get_identity(channel: str, handle: str) -> dict | None:
    """指定 handle の最新 identity bundle を返す。crash 推論を含む。

    crash 推論: 最新の event:state が terminal でない（loading/ready/working/blocked/
               escalated/draining）かつ最後の event:heartbeat 受信時刻から閾値超過 →
               メモリ上で inferred_cause を付与（DB 不変）。

    Returns:
        dict | None: identity bundle ＋ {msg_id, identity_at, inferred_cause?}
    """
    latest = _latest_events_by_type(channel, handle, ("identity", "state", "heartbeat"))
    identity_msg = latest.get("identity")
    if identity_msg is None:
        return None
    data = identity_msg["body"].get("data", {})
    result = dict(data)
    result["msg_id"] = identity_msg["msg_id"]
    result["identity_at"] = identity_msg["created_at"]

    state_msg = latest.get("state")
    workload_state = (
        state_msg["body"].get("data", {}).get("state") if state_msg is not None else None
    )
    heartbeat_msg = latest.get("heartbeat")
    last_heartbeat_at = heartbeat_msg["created_at"] if heartbeat_msg is not None else None

    inferred_cause = _infer_crash_cause(workload_state, last_heartbeat_at)
    if inferred_cause is not None:
        result["inferred_cause"] = inferred_cause

    return result


def ow_list_identities(channel: str, alive_only: bool = False) -> list[dict]:
    """channel 上の全 handle の identity リスト。

    alive_only=True の場合:
    - identity bundle に terminated_at / cause(closed/cancelled/dead) を持つ entry を除外
    - 加えて、ow_get_identity と同様の crash 推論（state が non-terminal + heartbeat 途絶）
      で inferred_cause が付与される entry も除外する
    handle フィールドが欠落した event は集約キーが None になるためスキップする。
    """
    history = ow_history(channel, since=0, limit=10000)
    if "error" in history:
        return []

    # handle別に identity / state / heartbeat の最新msgを収集
    identity_by_handle: dict[str, dict] = {}
    state_by_handle: dict[str, dict] = {}
    heartbeat_by_handle: dict[str, dict] = {}
    for msg in history.get("messages", []):
        parsed = _parse_ow_event(msg)
        if parsed is None:
            continue
        h = parsed["handle"]
        if h is None:
            continue
        if parsed["body"].get("kind") != "event":
            continue
        t = parsed["body"].get("data", {}).get("type")
        if t == "identity":
            current = identity_by_handle.get(h)
            if current is None or parsed["msg_id"] > current["msg_id"]:
                identity_by_handle[h] = parsed
        elif t == "state":
            current = state_by_handle.get(h)
            if current is None or parsed["msg_id"] > current["msg_id"]:
                state_by_handle[h] = parsed
        elif t == "heartbeat":
            current = heartbeat_by_handle.get(h)
            if current is None or parsed["msg_id"] > current["msg_id"]:
                heartbeat_by_handle[h] = parsed

    entries = []
    for h, parsed in identity_by_handle.items():
        data = parsed["body"].get("data", {})
        entry = dict(data)
        entry["msg_id"] = parsed["msg_id"]
        entry["identity_at"] = parsed["created_at"]

        state_msg = state_by_handle.get(h)
        workload_state = (
            state_msg["body"].get("data", {}).get("state")
            if state_msg is not None
            else None
        )
        hb_msg = heartbeat_by_handle.get(h)
        last_heartbeat_at = hb_msg["created_at"] if hb_msg is not None else None
        inferred_cause = _infer_crash_cause(workload_state, last_heartbeat_at)
        if inferred_cause is not None:
            entry["inferred_cause"] = inferred_cause

        if alive_only:
            cause = entry.get("cause")
            if cause in ("closed", "cancelled", "dead") or entry.get("terminated_at"):
                continue
            if inferred_cause is not None:
                continue

        entries.append(entry)

    return entries


def ow_get_presence(channel: str, handle: str) -> dict:
    """最新 heartbeat 受信時刻から online/offline を推論する。

    ow_recover の _get_presence() と推論ロジックを統一した実装。
    SSE 接続状態ではなく heartbeat 時刻ベースの推論を行う。

    heartbeat が一度も観測されない handle に対しては status="unknown" の
    entry を返す（None は返さない）。

    Returns:
        dict: {handle, status("online"|"offline"|"unknown"), last_heartbeat_at, phase}
    """
    heartbeat_msg = _query_latest_event(channel, handle, "heartbeat")
    if heartbeat_msg is None:
        return {"handle": handle, "status": "unknown", "last_heartbeat_at": None, "phase": None}

    last_heartbeat_at = heartbeat_msg["created_at"]
    phase = heartbeat_msg["body"].get("data", {}).get("phase")
    timeout = _HEARTBEAT_TIMEOUT_SECS.get(phase or "", _HEARTBEAT_TIMEOUT_DEFAULT)

    try:
        hb_time = datetime.fromisoformat(last_heartbeat_at)
        if hb_time.tzinfo is None:
            hb_time = hb_time.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - hb_time).total_seconds()
        status = "online" if elapsed < timeout else "offline"
    except (ValueError, TypeError):
        status = "unknown"

    return {
        "handle": handle,
        "status": status,
        "last_heartbeat_at": last_heartbeat_at,
        "phase": phase,
    }


def ow_get_workload_state(channel: str, handle: str) -> dict | None:
    """指定 handle の最新 workload state を返す。"""
    state_msg = _query_latest_event(channel, handle, "state")
    if state_msg is None:
        return None
    data = state_msg["body"].get("data", {})
    return {
        "handle": handle,
        "state": data.get("state"),
        "cause": data.get("cause"),
        "msg_id": state_msg["msg_id"],
        "state_at": state_msg["created_at"],
    }
