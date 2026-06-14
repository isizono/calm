"""ow（orch/worker）基盤サービス

relay HTTPサーバーとのやり取り、worker spawn/close、ステータス管理を担う。
外部HTTPのためcc-memory DBのconn共有パターンは不要。urllib.requestベース（サードパーティ依存なし）。

relayサーバーはcc-memoryリポ内のsrc/relay/にvendoringされており、ow_serviceと
PROTOCOL_VERSIONを構造的に共有する。ensure_relay_serverは/healthのversion不一致時に
古いrelayをkillして再起動する自己修復gate（D#2481）。
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
    /healthレスポンスをそのまま返す（D#2481）。
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
    既存なら何もせず、未存在なら作成する（D#2453）。

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
# T1: ow_send
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
        body: ow固有JSON（{"v":1, "kind":"cmd"|"state", ...}）
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
# T2: ow_history
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
# T3: ow_spawn_worker / ow_close_worker
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
    permission: str = "",
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
    if model or permission:
        fields.append(("model", f"{model} / permission: {permission}"))
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
    permission: str,
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
        "permission_mode": permission,
        "timeout_min": timeout_min,
        "activity_id": activity_id,
        "topic_id": topic_id,
    }
    fm_yaml = yaml.safe_dump(fm_data, default_flow_style=False, allow_unicode=True, sort_keys=False)

    body_lines = [f"# {fm_data['task']}: {task_title}".rstrip()]
    if acceptance:
        body_lines += ["", "## Acceptance", "", acceptance]
    if context:
        body_lines += ["", "## Context", "", context]
    if playbook:
        body_lines += ["", "## Playbook", "", playbook]
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
    permission: str = "auto",
    task_title: str = "",
    acceptance: str = "",
    context: str = "",
    playbook: str = "",
    timeout_min: int = 60,
    activity_id: int | None = None,
    topic_id: str | None = None,
    task_n: int = 1,
) -> dict:
    """workerセッションを起動する。

    処理順: queueへspawning write-ahead → task file書き出し → アダプタ呼び出し → 安定ID返却

    Args:
        alias: workerのhandle（例: "w-a"）
        channel: channelコード
        cwd: workerの作業ディレクトリ
        model: 使用モデル（例: "sonnet", "opus"）
        permission: permission_mode（デフォルト: "auto"）。autoは全操作を自動承認するため、orchが管理する信頼されたタスクでの使用を前提とする
        task_title: タスクタイトル
        acceptance: 完了条件
        context: タスクコンテキスト
        playbook: プレイブック抜粋
        timeout_min: タイムアウト（分）
        activity_id: 対応するアクティビティID
        topic_id: 対応するトピックID
        task_n: タスク番号（Tn）

    Returns:
        {"term_ref": str, "task_file": str, "spawning": "ok"}
        manualフォールバック時: {"command": str, "manual": True, "task_file": str}
    """
    # relayサーバー確認
    if not ensure_relay_server():
        return {"error": {"code": "RELAY_UNAVAILABLE", "message": "relay server is not available"}}

    # channel存在確認 → 未存在なら自動作成
    if not ensure_channel(channel):
        return {"error": {"code": "CHANNEL_UNAVAILABLE", "message": f"channel {channel} could not be created"}}

    queue_dir = _get_queue_dir()
    task_dir = queue_dir / "tasks"

    # queueへspawning write-ahead（孤児worker対策 D#2395）
    orch_cwd = os.environ.get("OW_ORCH_CWD", "")
    if not orch_cwd:
        orch_cwd = os.getcwd()
        logger.warning(
            "OW_ORCH_CWD not set, using cwd=%s as orch_cwd. "
            "Crash recovery requires the same cwd (D#2394).",
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
            permission=permission,
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
        permission=permission,
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

    worker_cmd = (
        f'env OW_ROLE=worker OW_ALIAS={shlex.quote(alias)} OW_CHANNEL={shlex.quote(channel)} '
        f'OW_TASK_FILE={shlex.quote(str(task_file))} '
        f'claude --model {shlex.quote(model)} --permission-mode {shlex.quote(permission)} '
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

    # アダプタ呼び出し — stdoutから安定IDを取得する（D#2400）
    try:
        result = subprocess.run(
            ["bash", str(adapter_path), "spawn", cwd, worker_cmd],
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
# T4: ow_status
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
                    # 1 orch = 1 topic（D#2383）のためtopic_id指定が原則。
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
