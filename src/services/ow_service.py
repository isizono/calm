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
from typing import Any

import yaml

from src.relay import PROTOCOL_VERSION
from src.services.ow.cache import (
    CURRENT_SCHEMA_VERSION,
    OwState,
    find_topic_id_by_channel,
    load_state,
    save_state,
)

logger = logging.getLogger(__name__)

# ----------------------------
# 設定定数
# ----------------------------

RELAY_URL = os.environ.get("RELAY_URL", "http://127.0.0.1:8765")

# orch 配下のタスクファイル / 退場ログ等を置くディレクトリ。queue.md は廃止 (D#2791)。
_OW_ORCH_DIR = Path.home() / ".cc-memory" / "ow" / "orch"

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

    現行方針: claude-opus-4-7 のみ許可。sonnet・haiku・opus-4-8 は全て拒否。
    opus エイリアス（"opus", "opus-4-7", "claude-opus-4-7", `[1m]` 付き等）は
    すべて "claude-opus-4-7" に正規化する。

    Returns:
        (正規化済みmodel, エラーメッセージ) のタプル。
        エラーなしの場合は (正規化済みmodel, None)。
    """
    m = model.lower().strip()

    # sonnet系は禁止（credit消費が大きい）。`claude-sonnet-4-6[1m]` のような [1m] 付きも等しく拒否する
    if "sonnet" in m:
        return "", (
            f"model '{model}' は使用できません。"
            " sonnet は禁止されています。代わりに claude-opus-4-7 を使ってください。"
        )

    # haiku系も禁止（worker・SAともに opus 4.7 で統一）
    if "haiku" in m:
        return "", (
            f"model '{model}' は使用できません。"
            " haiku は禁止されています。代わりに claude-opus-4-7 を使ってください。"
        )

    # opus-4-8 は禁止（恒久ルール）
    if "opus-4-8" in m or "opus4-8" in m:
        return "", (
            f"model '{model}' は使用できません。"
            " opus 4.8 は禁止されています。代わりに claude-opus-4-7 を使ってください。"
        )

    # opus 系は claude-opus-4-7 に正規化
    if "opus" in m:
        return "claude-opus-4-7", None

    # 上記いずれにも該当しないモデルは拒否
    return "", (
        f"model '{model}' は使用できません。"
        " claude-opus-4-7 のみ許可されています。"
    )


# 思考worker (thinking worker) の effort enum。Claude のreasoning effort と対応する4段。
# None = 通常 worker。値が指定された場合は task_file 本文に思考トリガー語マーカー (正規綴り
# `ultrathink`) が埋め込まれ、OW_TERMINAL=tmux では split-pane ではなく new-window で
# 別タブに開かれる。
THINKING_EFFORTS: frozenset[str] = frozenset({"high", "xhigh", "max", "ultrathink"})

# orch 側コード/skill/ドキュメントから sentinel 綴り `ultratink` を渡せるよう、最大段の
# alias を1つ受け付ける。orch セッションが MCP 呼び出し時に正規綴り
# `ultrathink` を入力すると orch 自身の extended thinking モードが暴発するため、
# orch は sentinel `ultratink` を使い、ow_service 側で正規綴りに畳む。
_EFFORT_ALIASES: dict[str, str] = {"ultratink": "ultrathink"}

# worker alias の書式制約。kebab-case（小文字英数字+ハイフン、先頭は英字、末尾は英数字、
# 連続ハイフン禁止）かつ最小長 8 文字以上。短すぎる alias は名前衝突や視認性低下を招き、
# queue/relay 識別子として再利用しづらいため一律で拒否する。
# alias の上限長は意図的に設けない（task_file / queue / relay messages に埋め込まれるが、
# 物理上限はファイルシステム側に委ねる。orch 運用上は kebab-case の自然な命名で十分短く収まる）。
_ALIAS_MIN_LENGTH: int = 8
_ALIAS_PATTERN: re.Pattern[str] = re.compile(r"^[a-z]([a-z0-9]|-(?!-))*[a-z0-9]$")


def _validate_alias_format(alias: str) -> str | None:
    """alias の書式検証。OK なら None、NG ならユーザー向けエラーメッセージ文字列を返す。

    検証項目:
        - 最小長: 8 文字以上
        - kebab-case: 小文字英数字とハイフンのみ。先頭は英字、末尾は英数字、連続ハイフン禁止
    """
    if not isinstance(alias, str) or not alias:
        return "alias must be a non-empty string"
    if len(alias) < _ALIAS_MIN_LENGTH:
        return (
            f"alias '{alias}' is too short "
            f"(length={len(alias)}, min={_ALIAS_MIN_LENGTH})"
        )
    if not _ALIAS_PATTERN.match(alias):
        return (
            f"alias '{alias}' does not match required kebab-case pattern "
            "(lowercase letters/digits/hyphen, start with letter, "
            "end with letter or digit, no consecutive hyphens)"
        )
    return None

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


# ----------------------------
# stagnation detector (sentinel.py) auto-start
# ----------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SENTINEL_SCRIPT = _PROJECT_ROOT / "scripts" / "ow" / "sentinel.py"
_SENTINEL_SCRIPT_REL = "scripts/ow/sentinel.py"


def _sentinel_log_path(channel_code: str) -> Path:
    """sentinel の stderr を書き出す追記ログのパス。

    `Bash(run_in_background=true)` 経由で起動された場合に stderr が失われると、
    起動失敗 / relay 接続エラー / stagnation 検知の証跡が残らないため、channel
    ごとに固定パスへ追記する。
    """
    return Path("/tmp") / f"sentinel-{channel_code}.log"


def _is_sentinel_running(channel_code: str) -> bool:
    """同 channel の sentinel.py プロセスが既に走っているか pgrep で判定する。

    pgrep が無い・タイムアウト等の例外時は False を返す (呼び出し側で spawn を
    試みる)。sentinel.py 自体は in-memory state + 冪等 polling なので最悪重複
    起動しても致命的にはならない。

    パターン末尾に `$` アンカーを付け、`ow1` を渡したときに `ow10` / `ow100` 等の
    prefix 衝突で誤検知するのを防ぐ。
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"{_SENTINEL_SCRIPT_REL}.*{channel_code}$"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def ensure_sentinel_process(channel_code: str) -> bool:
    """ow stagnation detector (scripts/ow/sentinel.py) を channel ごとに 1 プロセスで起動する。

    orch が `ow_status` を呼ぶたびに通過するため、AI セッションが SKILL.md の
    起動手順を読み飛ばしても sentinel が自動的に立ち上がる (D#2752 Phase A の
    起動配線、PR #432)。

    - 既に同 channel の sentinel が pgrep で見つかれば何もしない (1 channel = 1 プロセス)
    - `OW_SKIP_SENTINEL_AUTOSPAWN=1` 環境変数で skip 可能 (test / 一時無効化用)
    - 起動失敗は logger.warning に流すだけで呼び出し元には伝播させない
      (`ow_status` を fail させてはいけない)
    - 起動コマンドは `uv run --directory <project_root> python scripts/ow/sentinel.py`。
      hooks/hooks.json の他 Python 呼び出しと一貫させ、将来 sentinel.py に外部依存
      が追加されても project venv で解決されるようにする。
    - sentinel の stderr は `/tmp/sentinel-<channel>.log` に追記する。診断ログ
      (起動失敗・relay 接続エラー・stagnation 検知) が捨てられないようにする。

    Returns:
        True なら起動成功 or 既に起動済み、False なら起動失敗 or skip。
    """
    if os.environ.get("OW_SKIP_SENTINEL_AUTOSPAWN") == "1":
        return False
    if not _SENTINEL_SCRIPT.is_file():
        logger.warning("sentinel script not found at %s — skip auto-start", _SENTINEL_SCRIPT)
        return False
    if _is_sentinel_running(channel_code):
        return True
    log_path = _sentinel_log_path(channel_code)
    try:
        log_fh = open(log_path, "ab")
    except OSError as exc:
        logger.warning("failed to open sentinel log %s: %s — fallback to DEVNULL", log_path, exc)
        log_fh = None
    try:
        subprocess.Popen(
            [
                "uv",
                "run",
                "--directory",
                str(_PROJECT_ROOT),
                "python",
                _SENTINEL_SCRIPT_REL,
                channel_code,
            ],
            cwd=str(_PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=log_fh if log_fh is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError as exc:
        logger.warning("failed to spawn sentinel for channel=%s: %s", channel_code, exc)
        return False
    finally:
        # Popen 側で fd は dup される。親プロセスのハンドルは閉じてよい。
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass


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


# ----------------------------
# ow_spawn_worker / ow_close_worker
# ----------------------------


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
    effort: str | None = None,
) -> Path:
    """task fileをマークダウン（YAML frontmatter + 本文）で書き出す。

    機械可読フィールド（task/alias/channel/cwd/model等）はfrontmatterに、
    人間可読な内容（タイトル・acceptance・context・playbook）は本文に置く。
    workerはfrontmatterから起動パラメータを、本文からタスク内容を読み取る。

    ファイル名は `t<topic_id>-T<n>-<title-slug>.md`。topic prefixでtopic間の名前衝突を、
    title slugで人間がファイルを開かずに内容を把握できることを担保する。
    topic_idが未指定の場合は `T<n>-<title-slug>.md`、slugが空なら接尾辞を省く。

    effort が指定された場合 (THINKING_EFFORTS の値) は思考workerとして扱い、本文冒頭
    （タイトル直後）に思考workerマーカーセクションを挿入する。マーカーには正規綴り
    `ultrathink` （claude の extended thinking トリガー語）を埋め込む。frontmatterにも
    `effort: <値>` を残す。

    Note: ドキュメント・skill・チャット文中で本トリガー語に言及する場合は、orch
    セッション側で extended thinking モードが暴発しないよう sentinel `ultratink`
    （意図的タイポ）を使う運用とする。worker 埋め込みの実体は正規綴り `ultrathink`。
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
    if effort:
        fm_data["effort"] = effort
    fm_yaml = yaml.safe_dump(fm_data, default_flow_style=False, allow_unicode=True, sort_keys=False)

    acceptance_clean = _sanitize_task_body_field(acceptance, "acceptance")
    context_clean = _sanitize_task_body_field(context, "context")
    playbook_clean = _sanitize_task_body_field(playbook, "playbook")

    body_lines = [f"# {fm_data['task']}: {task_title}".rstrip()]
    if effort:
        body_lines += [
            "",
            "## Thinking worker",
            "",
            "ultrathink",
            "",
            f"このタスクは思考worker (effort: {effort}) 扱い。実装ではなく深い議論・"
            "設計検討・調査を行う。上記キーワード `ultrathink` は claude の extended "
            "thinking モードトリガー語。worker セッションは長考モードで動作する。",
        ]
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


def _resolve_activity_title(activity_id: int) -> str:
    """activities.id → title を返す。見つからない/エラー時は空文字。

    spawn時にtask_title未指定でもactivity_idがあればAPI呼び出しを増やさずタイトルを引ける。
    DBアクセス失敗はspawn全体を止めない（best-effort）。
    """
    try:
        from src.db import get_connection

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title FROM activities WHERE id = ?", (activity_id,)
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return str(row[0])
    except Exception as e:
        logger.warning("activity title lookup failed for id=%s: %s", activity_id, e)
    return ""


def _ensure_worker_askuser_deny(cwd: str) -> None:
    """worker の cwd 配下に `.claude/settings.local.json` を用意し、
    `permissions.deny` に `"AskUserQuestion"` を追記する。

    既存ファイルがあれば JSON ロードしてマージ（permissions.deny リストへ append + dedup）。
    存在しなければ新規作成。書き出しは UTF-8 + 末尾改行。

    Claude Code CLI の permission deny を使い、worker セッションが AskUserQuestion を
    呼び出した場合に標準挙動として遮断させる狙い。

    cwd が存在しない・書き込み不能などの I/O エラー時は warning ログのみで黙って続行し、
    spawn 全体は失敗させない（cwd 健全性は `_validate_spawn_preconditions` 側の責務）。
    """
    try:
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.is_dir():
            logger.warning(
                "_ensure_worker_askuser_deny: cwd %s is not a directory, skipping",
                cwd,
            )
            return
        settings_dir = cwd_path / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = settings_dir / "settings.local.json"

        data: dict = {}
        if settings_path.exists():
            try:
                raw = settings_path.read_text(encoding="utf-8")
                if raw.strip():
                    loaded = json.loads(raw)
                    if isinstance(loaded, dict):
                        data = loaded
                    else:
                        logger.warning(
                            "_ensure_worker_askuser_deny: %s is not a JSON object, "
                            "overwriting", settings_path,
                        )
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(
                    "_ensure_worker_askuser_deny: failed to read %s (%s), overwriting",
                    settings_path, e,
                )
                data = {}

        permissions = data.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
            data["permissions"] = permissions
        deny = permissions.get("deny")
        if not isinstance(deny, list):
            deny = []
        if "AskUserQuestion" not in deny:
            deny.append("AskUserQuestion")
        permissions["deny"] = deny

        settings_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(
            "_ensure_worker_askuser_deny: I/O error writing settings.local.json "
            "under %s: %s", cwd, e,
        )


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
    effort: str | None = None,
) -> dict:
    """workerセッションを起動する。

    処理順: spawn前ヘルスチェック → relay へ spawning event broadcast → task file書き出し
        → アダプタ呼び出し → 安定ID返却

    permission_modeは常にautoに固定される。

    Args:
        alias: workerのhandle（例: "w-a"）
        channel: channelコード
        cwd: workerの作業ディレクトリ
        model: 使用モデル（claude-opus-4-7 のみ許可。"opus", "opus-4-7" 等の
            エイリアスは正規化される。sonnet/haiku/opus-4-8 はバリデーションで拒否）
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
        effort: 思考worker（深い議論・設計検討・調査向けworker）として起動するなら
            THINKING_EFFORTS の値 (`"high"` / `"xhigh"` / `"max"` / `"ultrathink"`) のい
            ずれかを指定する。指定時は task_file 本文に正規綴り `ultrathink` マーカー
            セクションが挿入され、frontmatter にも `effort: <値>` が残る。OW_TERMINAL=
            tmux のときは split-pane ではなく `tmux new-window` で別タブに開く。role は
            worker のまま（新role不要）。対応activityには `intent:thinking` タグを別途
            付与すること。None（デフォルト）は通常 worker。

    Returns:
        {"term_ref": str, "task_file": str, "spawning": "ok"}
        manualフォールバック時: {"command": str, "manual": True, "task_file": str}
        spawn前検証失敗時: {"error": {"code": "SPAWN_PRECONDITION_FAILED", "warnings": [...]}}
        effort不正値時: {"error": {"code": "INVALID_EFFORT", "message": ...}}
    """
    # effort validation. sentinel alias (`ultratink`) を正規綴りに畳んでから検証する。
    if effort is not None:
        effort = _EFFORT_ALIASES.get(effort, effort)
        if effort not in THINKING_EFFORTS:
            return {
                "error": {
                    "code": "INVALID_EFFORT",
                    "message": (
                        f"effort '{effort}' は不正値。許可値: "
                        f"{sorted(THINKING_EFFORTS)} (sentinel: {sorted(_EFFORT_ALIASES)}), "
                        "または None"
                    ),
                },
            }

    # model validation / normalization
    model, model_error = _normalize_and_validate_model(model)
    if model_error:
        return {
            "error": {
                "code": "INVALID_MODEL",
                "message": model_error,
            },
        }

    # task_title未指定 かつ activity_id指定時は activities.title を自動解決する。
    # 呼び出し側（orch等）に task_title を毎回詰めさせず、activity名と一貫させる。
    # この task_title は task file frontmatter / ファイル名スラッグ / worker_cmd の
    # --name（セッション表示名）すべてに伝播する。
    if not task_title and activity_id is not None:
        task_title = _resolve_activity_title(activity_id)

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

    # worker workspace に `.claude/settings.local.json` を用意し AskUserQuestion を deny する。
    # Claude Code CLI の permission deny を使って worker セッションから AskUserQuestion を
    # 構造的に遮断する。既存設定があればマージ。
    _ensure_worker_askuser_deny(cwd)

    task_dir = _OW_ORCH_DIR / "tasks"

    # relay へ spawning broadcast (孤児 worker 対策の真実源化、SKILL.md §通信プロトコル orch→broadcast)。
    # projector は本 event を受信して cache.workers[alias].task_status="spawning" を書き、
    # event:identity / event:state(loading) を送る前に worker が crash しても relay event で復元可能になる。
    spawning_body = {
        "v": 1,
        "kind": "event",
        "from": "orch",
        "to": "*",
        "task": f"T{task_n}",
        "data": {
            "type": "state",
            "state": "spawning",
            "target_handle": alias,
            "spawning_at": datetime.now(timezone.utc).isoformat(),
            "activity_id": activity_id,
            "cwd": cwd,
            "model": model,
            "acceptance": acceptance,
        },
    }
    spawning_result = ow_send(channel=channel, handle="orch", body=spawning_body)
    if "error" in spawning_result:
        err_detail = spawning_result.get("error")
        logger.error(
            "ow_spawn_worker: failed to broadcast event:state(spawning) for %s: %s — aborting spawn",
            alias,
            err_detail,
        )
        return {
            "error": {
                "code": "SPAWN_PRECONDITION_FAILED",
                "message": "relay broadcast of event:state(spawning) failed; spawn aborted",
                "warnings": [
                    f"relay broadcast event:state(spawning) failed for {alias}: {err_detail}"
                ],
            },
        }

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
        effort=effort,
    )

    # アダプタ起動
    # OW_TERMINAL 未設定時は "tmux" にフォールバックする (原則 tmux 運用)。
    # 明示的に "manual" 指定された場合のみ起動コマンドを返す手動フォールバック経路に入る。
    terminal = os.environ.get("OW_TERMINAL", "tmux")
    adapter_path = _get_adapter_path(terminal) if terminal != "manual" else None

    # --add-dir は commander.js の variadic option (`<directories...>`) で、空白区切り形式
    # (`--add-dir DIR PROMPT`) だと続く positional prompt を dir として吸収する。
    # `=` 形式 (`--add-dir=DIR`) は単一値として確定的にパースされ、複数指定は
    # `--add-dir=DIR1 --add-dir=DIR2` の append 形で渡せる。後続 prompt の吸収リスクが
    # 構造的に発生しないため、`--` separator なしで positional が確実に届く。
    # --name はセッション表示名（プロンプトボックス・/resume picker・端末タイトル）。
    # workerはtask_titleをActivity名としてそのまま渡し、orch側で見分けやすくする。
    session_name = task_title or alias
    # OW_PARENT_PID=$$ + exec claude で「shell PID → claude PID」の継承を行い、
    # claude プロセスに OW_PARENT_PID 環境変数として自身の PID を埋め込む。
    # claude の子（Bash tool / Monitor で起動される recv.sh / heartbeat.sh）はこの env を
    # 継承するため、claude 本体死亡時に watchdog が自動 exit する。
    # worker SKILL.md 依存ゼロ（A案 + ow_service spawn 経路で完結）。
    #
    # 注: `$$` はこの Python 文字列ではエスケープされず、tmux.sh の `eval` 実行時
    # （base64 経由で運搬された後）に「その bash プロセスの PID」へ展開される。
    # 直後の `exec claude ...` で claude が同じ PID を引き継ぐため、`$$` は
    # 結果的に claude 本体の PID と一致する。詳細は tmux.sh の eval 周辺コメント参照。
    worker_cmd = (
        f'OW_PARENT_PID=$$ '
        f'OW_ROLE=worker OW_ALIAS={shlex.quote(alias)} OW_CHANNEL={shlex.quote(channel)} '
        f'OW_TASK_FILE={shlex.quote(str(task_file))} '
        f'exec claude --model {shlex.quote(model)} --permission-mode auto '
        f'--name {shlex.quote(session_name)} '
        f'--add-dir={shlex.quote(str(task_file.parent))} '
        f'{shlex.quote(f"workerスキルに従って作業を開始して。task: {task_file}")}'
    )

    if adapter_path is None:
        # manualフォールバック: 起動コマンドを返す
        # adapter_path不在のとき、payloadだけ見ると後段の追跡で原因が分からなくなる。
        # adapter_errorに「どのterminalで・どこを探したか」を明示する。
        expected_adapter = (
            Path(__file__).resolve().parent.parent.parent
            / "scripts" / "ow" / "adapters" / f"{terminal}.sh"
        )
        adapter_error = (
            f"adapter not found at {expected_adapter} (OW_TERMINAL={terminal!r})"
        )
        logger.error("ow_spawn_worker manual fallback: %s", adapter_error)
        return {
            "command": worker_cmd,
            "manual": True,
            "task_file": str(task_file),
            "alias": alias,
            "adapter_error": adapter_error,
        }

    # アダプタ呼び出し — stdoutから安定IDを取得する
    # tmux アダプタは positional 引数 `[target_pane] [is_thinking]` を受ける:
    #   - is_thinking=1 のとき split-pane ではなく `tmux new-window` で別タブ起動
    #   - target_pane が無い思考worker のときは空文字列をプレースホルダにして is_thinking のみ届ける
    adapter_args = ["bash", str(adapter_path), "spawn", cwd, worker_cmd]
    if terminal == "tmux":
        is_thinking = "1" if effort is not None else "0"
        if tmux_target_pane:
            adapter_args.extend([tmux_target_pane, is_thinking])
        elif effort is not None:
            adapter_args.extend(["", is_thinking])
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
        logger.error("ow_spawn_worker manual fallback: adapter spawn timed out after 30s")
        return {
            "command": worker_cmd,
            "manual": True,
            "task_file": str(task_file),
            "alias": alias,
            "adapter_error": "adapter spawn timed out",
        }
    except subprocess.CalledProcessError as e:
        logger.error("ow_spawn_worker manual fallback: adapter spawn failed: %s", e.stderr)
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
        term_ref: 安定ID（tmux の pane ID 等）

    Returns:
        {"closed": True} または {"error": ...}
    """
    # OW_TERMINAL 未設定時は "tmux" にフォールバックする (原則 tmux 運用)。
    # ただし term_ref が "manual:" prefix の場合は手動起動された worker なので、
    # env_terminal に関係なく manual 経路に倒す (アダプタが解釈不能な term_ref を
    # 渡してサイレントに失敗するのを防ぐ)。
    env_terminal = os.environ.get("OW_TERMINAL", "tmux")
    forced_manual_by_term_ref = (
        env_terminal != "manual" and classify_term_ref(term_ref) == "manual"
    )
    terminal = "manual" if forced_manual_by_term_ref else env_terminal
    adapter_path = _get_adapter_path(terminal) if terminal != "manual" else None

    if adapter_path is None:
        if forced_manual_by_term_ref:
            adapter_error = (
                f"manual fallback due to term_ref format "
                f"(OW_TERMINAL={env_terminal!r}, term_ref={term_ref!r})"
            )
        else:
            expected_adapter = (
                Path(__file__).resolve().parent.parent.parent
                / "scripts" / "ow" / "adapters" / f"{terminal}.sh"
            )
            adapter_error = (
                f"adapter not found at {expected_adapter} (OW_TERMINAL={terminal!r})"
            )
        logger.error("ow_close_worker manual fallback: %s", adapter_error)
        return {
            "manual": True,
            "message": f"手動でterm_ref={term_ref}のセッションをクローズしてください",
            "adapter_error": adapter_error,
        }

    try:
        result = subprocess.run(
            ["bash", str(adapter_path), "close", term_ref],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        # tmux.sh は stdout 最終行に "closed" / "killed" のいずれかを返す契約。
        # 旧アダプタ (stdout 空) や manual 経路は後方互換で closed=True 扱いに倒す。
        stdout_lines = (result.stdout or "").strip().splitlines()
        last = stdout_lines[-1] if stdout_lines else ""
        if last == "killed":
            logger.warning(
                "ow_close_worker: pane survived kill-pane, SIGKILL fallback succeeded (term_ref=%s)",
                term_ref,
            )
            return {"closed": True, "killed": True, "term_ref": term_ref}
        if last == "closed":
            return {"closed": True, "killed": False, "term_ref": term_ref}
        # 旧アダプタ (stdout 空) / manual 経路: killed 不明だが closed 成功扱い。
        # 呼び出し側が result["killed"] で KeyError にならないよう False を埋める。
        return {"closed": True, "killed": False, "term_ref": term_ref}
    except subprocess.TimeoutExpired:
        logger.error("ow_close_worker adapter close timed out after 15s")
        return {
            "closed": False,
            "error": {"code": "ADAPTER_CLOSE_TIMEOUT", "message": "adapter close timed out"},
            "term_ref": term_ref,
        }
    except subprocess.CalledProcessError as e:
        logger.error("ow_close_worker adapter close failed: %s", e.stderr)
        return {
            "closed": False,
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


def ow_status(channel: str, topic_id: str | None = None) -> dict:
    """cache (派生1) + presence の統合ビュー (D#2751、SKILL.md §状態取得経路)。

    cache は relay events を真実源として projector が再構築する派生キャッシュ。
    本関数は load_state → cache miss なら projector で再構築する fallback 経路で
    取得し、cache.workers 各 handle のサマリを ``tasks`` リストに整形する。

    Args:
        channel: channelコード（presence取得に使用）
        topic_id: cache 対象 topic。文字列も整数も受け付ける。None の場合は
            cache ディレクトリを走査して channel に紐づく topic_id を逆引きする。

    Returns:
        {
            "tasks": [{task, alias, state, task_status, latest_msg_id, latest_at, online}, ...],
            "presence": [...],
            "summary": {"total_tasks": int, "status_counts": dict, "online_workers": [...]}
        }
    """
    if not ensure_relay_server():
        return {"error": {"code": "RELAY_UNAVAILABLE", "message": "relay server is not available"}}

    if channel:
        if not ensure_channel(channel):
            return {"error": {"code": "CHANNEL_UNAVAILABLE", "message": f"channel {channel} could not be created"}}
        # stagnation detector (sentinel.py) を auto-start する (PR #432, D#2752 Phase A)。
        # orch 起動時に必ず通る経路なので、AI が SKILL.md を読み飛ばしても sentinel が起動される。
        ensure_sentinel_process(channel)

    presence_result = _relay_request("GET", f"/presence?{urllib.parse.urlencode({'channel': channel})}")
    if "error" in presence_result:
        handles = []
    else:
        handles = presence_result.get("handles", [])

    # cache から worker サマリを構築する。
    topic_id_int: int | None = None
    if topic_id is not None:
        try:
            topic_id_int = int(str(topic_id))
        except (TypeError, ValueError):
            topic_id_int = None
    if topic_id_int is None:
        topic_id_int = find_topic_id_by_channel(channel)

    tasks: list[dict] = []
    if topic_id_int is not None:
        state = get_or_rebuild_state(topic_id_int, channel)
        if state is not None:
            for handle, worker in (state.get("workers") or {}).items():
                tasks.append(
                    {
                        "task": worker.get("task", ""),
                        "alias": handle,
                        "state": worker.get("state", ""),
                        "task_status": worker.get("task_status", ""),
                        "latest_msg_id": worker.get("latest_msg_id", 0),
                        "latest_at": worker.get("latest_at", ""),
                        "online": handle in handles,
                    }
                )

    status_counts: dict[str, int] = {}
    for task in tasks:
        s = task.get("task_status") or task.get("state") or "unknown"
        status_counts[s] = status_counts.get(s, 0) + 1

    return {
        "tasks": tasks,
        "presence": handles,
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
#   - 突合ロジックは relay messages history + cache OwState の 2 者突合で成立する
#     (旧 queue × relay × presence の 3 者突合から、cache 集約により縮減)
#   - reconstruct_state_from_relay と detect_crash_inconsistencies は純粋関数として実装し、テスト容易性を確保
#
# 突合分類:
#   - ghost_active: cache.workers 活動中 (working/blocked/escalated/draining) かつ presence offline → relay 最新state から再構築候補
#   - stalled_done: cache.workers 終端 (done/cancelled/failed) かつ presence online → ping 送信で素性照会
#   - orphans:    cache.workers 外で presence online の worker handle → ping 送信で再リンク照会


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


# cache.workers[alias].state における「workerが活動中」と分類する workload state。
# spawning は workload state ではなく task_status のため別カテゴリで扱う。
_ACTIVE_WORKLOAD_STATES: frozenset[str] = frozenset({"ready", "working", "blocked", "escalated", "draining"})

# cache.workers[alias].task_status における「workerが活動中」と分類する状態。
# spawning を含めることで、cache に spawning entry が残っている alias への重複 spawn を
# _validate_spawn_preconditions step 5 で検出できる。
_ACTIVE_TASK_STATUSES: frozenset[str] = frozenset({"working", "awaiting_verify", "escalated", "spawning"})

# cache.workers[alias].task_status における「明示的に終端済み」を示す値。
_TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"done", "cancelled", "failed"})

# identity bundle の cause として「明示的に終了済み」を示す値。
# alive判定（INV-9）や alive_only フィルタで同じ集合を参照するため一元化する。
_TERMINATED_IDENTITY_CAUSES: frozenset[str] = frozenset({"closed", "cancelled", "dead"})

# projector: worker からの event:state(state) を受信したときに cache.workers[alias].task_status へ
# マップする規則。設計書 v3 §5 と SKILL.md §projector マッピング表 に対応。
_STATE_TO_TASK_STATUS: dict[str, str] = {
    "loading": "spawning",
    "ready": "spawning",
    "working": "working",
    "blocked": "working",
    "escalated": "escalated",
    "draining": "working",
    "done": "awaiting_verify",
}

# event:state(terminated) の cause → cache.workers[alias].task_status マップ。
_CAUSE_TO_TASK_STATUS: dict[str, str] = {
    "closed": "done",
    "cancelled": "cancelled",
    "dead": "failed",
}

# relay 最新 state 宣言 → cache task_status の suggested 反映マップ。
# presence offline & cache active の ghost_active ケースで使う。
# 終端 state (done/closed/cancelled/failed) はそのまま反映、非終端 (ready/working等) は
# presence offline = 異常終了とみなして "stalled" に倒す（手動介入を促す）。
_RELAY_STATE_TO_TASK_STATUS: dict[str, str] = {
    "done": "done",
    "closed": "done",   # 旧プロトコル互換 (現行 worker は terminated/cause=closed で送るため state="closed" は通常来ない)
    "failed": "failed",
    "cancelled": "cancelled",
    "ready": "stalled",
    "working": "stalled",
    "blocked": "stalled",
    "escalated": "stalled",
    "draining": "stalled",
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
        - alias重複: 同aliasがpresence onlineまたはcache上で活動中タスクのworkerとして他の
          task_nに割当て済みでないか（同一task_nで再spawn=再リンクは許可）

    Returns:
        {
            "ok": bool,
            "warnings": [str],  # 失敗した検証項目のメッセージ一覧
        }

    呼び出し元はok=Falseならspawnを中止し、warningsをユーザー/orchに見せる責務を持つ。
    """
    warnings: list[str] = []

    # 0. alias書式（最小長 + kebab-case）。relay 接続前に純粋な書式検証で弾く。
    # 書式エラー時は relay 接続を行わずに早期return（無効aliasで relay 接続を発生させない）。
    alias_err = _validate_alias_format(alias)
    if alias_err is not None:
        warnings.append(alias_err)
        return {"ok": False, "warnings": warnings}

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

    # 5. alias 重複 (cache 由来 active task)。cache.workers[alias] が active な
    # task_status で、かつ別 task_n を持つ場合に警告する。
    if topic_id is not None:
        try:
            topic_id_int = int(str(topic_id))
        except (TypeError, ValueError):
            topic_id_int = None
        state = load_state(topic_id_int, channel) if topic_id_int is not None else None
        if state is not None:
            worker = (state.get("workers") or {}).get(alias)
            if worker is not None:
                w_state = worker.get("state", "")
                w_task = worker.get("task", "")
                w_task_status = worker.get("task_status", "")
                # 終端 / spawning は重複判定対象外
                is_active = w_state in _ACTIVE_WORKLOAD_STATES or w_task_status in _ACTIVE_TASK_STATUSES
                if is_active:
                    same_task = task_n is not None and w_task == f"T{task_n}"
                    if not same_task:
                        warnings.append(
                            f"alias {alias} already has active task {w_task} (state={w_state})"
                        )

    # 6. identity alive check (INV-9: 同一 handle で alive 期間が時間的に重複しない)
    identity = ow_get_identity(channel, alias)
    if identity is not None:
        cause = identity.get("cause")
        inferred_cause = identity.get("inferred_cause")
        terminated_at = identity.get("terminated_at")
        is_terminated = (
            cause in _TERMINATED_IDENTITY_CAUSES
            or bool(terminated_at)
            or inferred_cause is not None
        )
        if not is_terminated:
            warnings.append(
                f"alias {alias} has an alive identity on channel {channel} "
                "(not yet terminated); cannot spawn duplicate worker (INV-9)"
            )

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


# ----------------------------
# projector: relay → OwState → save_state
# ----------------------------


def project_state_to_cache(topic_id: int, channel: str) -> OwState | None:
    """relay full pull → OwState 構築 → save_state を呼ぶ projector 経路。

    1回の ``ow_history(since=0)`` 全件取得から、handle 単位の最新 state /
    identity / heartbeat を集計して :class:`OwState` を組み立て、
    :func:`save_state` でファイル先 (cache JSON) に書き出す。

    Args:
        topic_id: cache ファイル ``topic-<id>.json`` を決めるための topic 識別子。
        channel: relay channel コード。OwState.channel に格納する。

    Returns:
        構築・保存できた :class:`OwState`。以下のいずれかで ``None`` を返す
        (cache ファイルは触らない):

          - relay HTTP エラー等で full pull に失敗した場合
          - cache ファイル書き出し (:func:`save_state`) が OSError で失敗した場合
    """
    history_limit = 10000
    history = ow_history(channel, since=0, limit=history_limit)
    if "error" in history:
        return None

    messages = history.get("messages", [])
    # limit に達した場合は古い state declarations が欠落して
    # cache が不完全になる可能性がある。状態の無音欠損リスクを少なくとも
    # logger.warning で可視化する。
    if len(messages) >= history_limit:
        logger.warning(
            "ow projector: relay history truncated at limit=%d for channel=%r topic=%s; "
            "oldest state declarations may be missing and cache may be incomplete",
            history_limit,
            channel,
            topic_id,
        )

    workers: dict[str, dict] = {}
    identity_by_handle: dict[str, dict] = {}
    state_by_handle: dict[str, dict] = {}
    heartbeat_by_handle: dict[str, dict] = {}
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
        t = data.get("type")
        handle = body.get("from") or ""
        if not handle:
            continue

        created_at = msg.get("created_at", "")

        if t == "state":
            state_val = data.get("state") or ""
            if not state_val:
                continue
            task = body.get("task") or ""
            # orch broadcast event:state(spawning, target_handle=...) は target_handle が
            # cache.workers のキーになる (worker 自身の identity 送信前から cache に entry を作るため)。
            if (
                state_val == "spawning"
                and handle == "orch"
                and isinstance(data.get("target_handle"), str)
                and data.get("target_handle")
            ):
                target = data["target_handle"]
                spawn_entry = workers.get(target)
                if spawn_entry is None or msg_id >= spawn_entry.get("latest_msg_id", 0):
                    workers[target] = {
                        "task": task,
                        "state": "loading",
                        "task_status": "spawning",
                        "latest_msg_id": msg_id,
                        "latest_at": created_at,
                        "assigned_at": data.get("spawning_at") or "",
                        "acceptance": data.get("acceptance") or "",
                        "model": data.get("model") or "",
                        "cwd": data.get("cwd") or "",
                    }
                continue

            entry = workers.get(handle)
            if entry is None or msg_id >= entry.get("latest_msg_id", 0):
                prev = entry or {}
                worker_entry: dict[str, Any] = {
                    "task": task,
                    "state": state_val,
                    "latest_msg_id": msg_id,
                    "latest_at": created_at,
                }
                # spawning 系メタを保持しつつ task_status をマップする
                for k in ("assigned_at", "acceptance", "model", "cwd"):
                    if prev.get(k):
                        worker_entry[k] = prev[k]
                task_status = _STATE_TO_TASK_STATUS.get(state_val)
                if state_val == "terminated":
                    cause = data.get("cause") or ""
                    task_status = _CAUSE_TO_TASK_STATUS.get(cause, task_status)
                    if cause:
                        worker_entry["cause"] = cause
                if task_status is None:
                    task_status = prev.get("task_status") or ""
                if task_status:
                    worker_entry["task_status"] = task_status
                workers[handle] = worker_entry
            current_state = state_by_handle.get(handle)
            if current_state is None or msg_id > current_state["msg_id"]:
                state_by_handle[handle] = {
                    "msg_id": msg_id,
                    "data": dict(data),
                    "created_at": created_at,
                }
        elif t == "identity":
            current = identity_by_handle.get(handle)
            if current is None or msg_id > current["msg_id"]:
                identity_by_handle[handle] = {
                    "msg_id": msg_id,
                    "data": dict(data),
                    "created_at": created_at,
                }
        elif t == "heartbeat":
            current = heartbeat_by_handle.get(handle)
            if current is None or msg_id > current["msg_id"]:
                heartbeat_by_handle[handle] = {
                    "msg_id": msg_id,
                    "data": dict(data),
                    "created_at": created_at,
                }

    # presence: heartbeat 時刻ベースの online 判定 (ow_get_presence と同ロジック)
    now = datetime.now(timezone.utc)
    presence: list[str] = []
    for handle, hb in heartbeat_by_handle.items():
        phase = (hb["data"].get("phase") if isinstance(hb.get("data"), dict) else None) or ""
        timeout = _HEARTBEAT_TIMEOUT_SECS.get(phase, _HEARTBEAT_TIMEOUT_DEFAULT)
        created_at = hb.get("created_at") or ""
        try:
            hb_time = datetime.fromisoformat(created_at)
            if hb_time.tzinfo is None:
                hb_time = hb_time.replace(tzinfo=timezone.utc)
            elapsed = (now - hb_time).total_seconds()
        except (ValueError, TypeError):
            continue
        if elapsed < timeout:
            presence.append(handle)

    # ``identities`` は raw data 形式 (handle → data dict)。
    # reducer fastpath 用には EventEntry 形式の ``identity_events`` を別途持つ
    # (msg_id / created_at が必要なため)。
    identities_raw = {h: e["data"] for h, e in identity_by_handle.items()}

    state: OwState = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "channel": channel,
        "last_msg_id": max_msg_id,
        "workers": workers,
        "identities": identities_raw,
        "identity_events": identity_by_handle,
        "states": state_by_handle,
        "heartbeats": heartbeat_by_handle,
        "presence": sorted(presence),
        "updated_at": now.isoformat(),
    }
    try:
        save_state(topic_id, state)
    except OSError as e:
        # ファイルシステムエラー (ディスク満杯、権限不足、I/O エラー等) を
        # 呼び出し側 (ow_status 等) に伝播させない。docstring の契約通り None を返す。
        logger.warning(
            "ow projector: save_state failed for topic=%s channel=%r: %s",
            topic_id,
            channel,
            e,
        )
        return None
    return state


def get_or_rebuild_state(topic_id: int, channel: str) -> OwState | None:
    """cache から load_state、None なら projector で再構築する自動 fallback ヘルパー。

    :func:`src.services.ow.cache.load_state` が以下のいずれかで ``None`` を返した
    場合に :func:`project_state_to_cache` を呼んで cache を再生成する:

      1. cache ファイル不存在 (cache miss)
      2. JSON corruption — ``load_state`` が削除して ``None``
      3. ``schema_version`` mismatch — 同上
      4. ``channel`` mismatch — 同上 (引数 ``channel`` を ``load_state`` に渡す)

    relay full pull に失敗した場合は ``None`` (cache は書き換えない)。
    """
    state = load_state(topic_id, channel=channel)
    if state is not None:
        return state
    return project_state_to_cache(topic_id, channel)



def _parse_spawning_at(value: str | None) -> datetime | None:
    """`- spawning: <iso>` フィールドからdatetime（tz-aware）を取り出す。

    queueファイルが手編集で壊れていても例外を投げない（fail-soft）。
    naive datetime はUTCとみなして tz-awareに格上げする。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def detect_crash_inconsistencies(
    cache_workers: dict[str, dict],
    reconstructed: dict,
    presence: list[str],
    pending_spawn_stalled_threshold_min: int | None = None,
    now: datetime | None = None,
) -> dict:
    """cache.workers × relay 最新 state × presence を突合し、不整合を 4 カテゴリに分類する。

    純粋関数（I/O なし）。テスト容易性のため cache_workers / relay / presence は全て引数で受け取る。

    Args:
        cache_workers: cache.workers dict。``{alias: {task, state, task_status, latest_msg_id,
            latest_at, assigned_at, ...}, ...}``。``OwState["workers"]`` の中身に対応する。
        reconstructed: ``reconstruct_state_from_relay`` の返り値
        presence: ``_get_presence`` の返り値
        pending_spawn_stalled_threshold_min: pending_spawn (has_relay_history=False) を
            ``auto_stalled`` 化する経過時間閾値（分）。None または ``assigned_at`` 不在/不正
            なら自動更新対象外。閾値以上経過していれば ``suggested_status="auto_stalled"`` を入れる。
        now: 経過時間判定の基準時刻。テスト用に inject 可能。None なら ``datetime.now(UTC)``。

    Returns:
        {
            "ghost_active": [    # cache 活動中だが presence offline
                {task, alias, task_status, latest_state, latest_msg_id, latest_at, suggested_status},
                ...
            ],
            "pending_spawn": [   # cache.task_status=spawning。relay履歴の有無で2通り
                {task, alias, task_status, has_relay_history, latest_state, latest_msg_id, latest_at,
                 assigned_at, age_min, suggested_status},
                ...
            ],
            "stalled_done": [    # cache 終端だが worker が presence に残存
                {task, alias, task_status},
                ...
            ],
            "orphans": [         # presence online だが cache.workers 外
                {alias, relay_tasks: [{task, latest_state, latest_msg_id}, ...]},
                ...
            ],
        }
    """
    by_wt = reconstructed.get("by_worker_task", {}) or {}
    presence_set = set(presence or [])
    now_dt = now or datetime.now(timezone.utc)

    ghost_active: list[dict] = []
    pending_spawn: list[dict] = []
    stalled_done: list[dict] = []
    cache_aliases: set[str] = set()

    for alias, worker_state in (cache_workers or {}).items():
        if not alias:
            continue
        task_status = (worker_state.get("task_status") or "") if isinstance(worker_state, dict) else ""
        state_val = (worker_state.get("state") or "") if isinstance(worker_state, dict) else ""
        task_id = (worker_state.get("task") or "") if isinstance(worker_state, dict) else ""
        cache_aliases.add(alias)

        relay_entry = by_wt.get(f"{alias}:{task_id}") if task_id else None

        if task_status == "spawning" and alias not in presence_set:
            has_history = relay_entry is not None
            latest_state = relay_entry["latest_state"] if relay_entry else None
            assigned_at_str = (worker_state.get("assigned_at") or "") if isinstance(worker_state, dict) else ""
            assigned_at_dt = _parse_spawning_at(assigned_at_str)
            age_min: float | None = None
            if assigned_at_dt is not None:
                age_min = (now_dt - assigned_at_dt).total_seconds() / 60.0

            if has_history:
                suggested = _RELAY_STATE_TO_TASK_STATUS.get(latest_state, "stalled")
            elif (
                pending_spawn_stalled_threshold_min is not None
                and age_min is not None
                and age_min >= pending_spawn_stalled_threshold_min
            ):
                suggested = "auto_stalled"
            else:
                suggested = None
            pending_spawn.append(
                {
                    "task": task_id,
                    "alias": alias,
                    "task_status": task_status,
                    "has_relay_history": has_history,
                    "latest_state": latest_state,
                    "latest_msg_id": relay_entry["latest_msg_id"] if relay_entry else 0,
                    "latest_at": relay_entry["latest_at"] if relay_entry else "",
                    "assigned_at": assigned_at_str,
                    "age_min": age_min,
                    "suggested_status": suggested,
                }
            )
        elif (
            (state_val in _ACTIVE_WORKLOAD_STATES or task_status in _ACTIVE_TASK_STATUSES)
            and alias not in presence_set
        ):
            latest_state = relay_entry["latest_state"] if relay_entry else None
            suggested = (
                _RELAY_STATE_TO_TASK_STATUS.get(latest_state, "stalled")
                if latest_state
                else "stalled"
            )
            ghost_active.append(
                {
                    "task": task_id,
                    "alias": alias,
                    "task_status": task_status,
                    "latest_state": latest_state,
                    "latest_msg_id": relay_entry["latest_msg_id"] if relay_entry else 0,
                    "latest_at": relay_entry["latest_at"] if relay_entry else "",
                    "suggested_status": suggested,
                }
            )
        elif task_status in _TERMINAL_TASK_STATUSES and alias in presence_set:
            stalled_done.append(
                {
                    "task": task_id,
                    "alias": alias,
                    "task_status": task_status,
                }
            )

    orphans: list[dict] = []
    for handle in sorted(presence_set):
        if not handle.startswith("w-"):
            continue
        if handle in cache_aliases:
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

    Returns:
        ow_send の戻り値に nonce フィールドを追加したdict。
        nonce は ow_recover の pending_pings 引数に渡すことで、
        次回呼び出し時に nonce echo の有無を確認できる。
    """
    nonce = uuid.uuid4().hex
    body = {
        "v": 1,
        "kind": "command",
        "from": "orch",
        "to": alias,
        "task": task or "T0",
        "data": {"type": "ping", "nonce": nonce, "recovery": True},
    }
    result = ow_send(channel=channel, handle="orch", body=body, needs_reply=True)
    # ow_send 失敗時は nonce を付与しない。送信が成立していないものを
    # pending_pings に積むと、後続の nonce echo チェックで "応答なし" と
    # "送信途中" が区別できなくなる。
    if "error" not in result:
        result["nonce"] = nonce
    return result


def _check_nonce_echo(channel: str, nonce: str, after_msg_id: int = 0) -> bool:
    """ping送信後のrelayでin_reply_nonceが一致するeventを探す。

    Args:
        channel: channelコード
        nonce: _send_recovery_pingが生成したnonce文字列
        after_msg_id: pingを送った直後のmsg_id（これより大きいメッセージのみ対象）

    Returns:
        True: 対応nonceを持つevent:heartbeatまたはevent:stateが見つかった
        False: 見つからなかった、またはrelay取得失敗
    """
    history = ow_history(channel, since=after_msg_id, limit=1000)
    if "error" in history:
        return False
    for msg in history.get("messages", []):
        body = msg.get("body", {})
        if not isinstance(body, dict):
            continue
        if body.get("kind") != "event":
            continue
        data = body.get("data") or {}
        if data.get("type") not in ("heartbeat", "state"):
            continue
        if data.get("in_reply_nonce") == nonce:
            return True
    return False


def ow_recover(
    channel: str,
    topic_id: str,
    dry_run: bool = False,
    pending_pings: list[dict] | None = None,
    pending_spawn_stalled_threshold_min: int | None = None,
    now: datetime | None = None,
) -> dict:
    """orch crash 後の cache × relay 履歴 2 者突合 + 自動修正のエントリポイント。

    手順:
        1. ensure_relay_server / ensure_channel で前提整える
        2. reconstruct_state_from_relay で全 relay 履歴から状態再構築
        3. get_or_rebuild_state で cache を取得（cache miss なら projector 再構築）
        4. _get_presence で現在 online の worker 一覧取得
        5. detect_crash_inconsistencies で 4 カテゴリ分類
        6. pending_pings が渡された場合: nonce echo 確認
        7. dry_run=False なら:
           - ghost_active + pending_spawn (suggested_status あり): cache を再構築して suggested_status を反映
           - stalled_done / orphans: command:ping 送信

    Args:
        channel: channelコード
        topic_id: トピックID（文字列も整数も受け付ける）
        dry_run: True なら検出のみ・修正 / 送信なし
        pending_pings: 前回の ping 送信情報。各要素は ``{alias, task, nonce, sent_after_msg_id}``
        pending_spawn_stalled_threshold_min: pending_spawn (has_relay_history=False) を
            ``auto_stalled`` 化する経過時間閾値（分）
        now: 経過時間判定の基準時刻。テスト用に inject 可能

    Returns:
        {
            "detected": {ghost_active, pending_spawn, stalled_done, orphans},
            "applied": {cache_updates: [...], pings_sent: [...]},
            "warnings": [str],
            "presence": [str],
            "reconstructed_max_msg_id": int,
            "dry_run": bool,
            "nonce_echo_results": [...],
        }
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

    try:
        topic_id_int = int(str(topic_id))
    except (TypeError, ValueError):
        topic_id_int = None
    topic_id_str = str(topic_id)

    reconstructed = reconstruct_state_from_relay(channel)
    if "error" in reconstructed:
        return {
            "detected": {
                "ghost_active": [],
                "pending_spawn": [],
                "stalled_done": [],
                "orphans": [],
            },
            "applied": {"cache_updates": [], "pings_sent": []},
            "warnings": [f"relay history fetch error: {reconstructed['error']}"],
            "presence": [],
            "reconstructed_max_msg_id": 0,
            "dry_run": dry_run,
            "nonce_echo_results": [],
        }
    if reconstructed.get("truncated"):
        warnings.append(
            "relay history truncated at limit=10000: oldest state declarations may be missing"
        )

    cache_workers: dict[str, dict] = {}
    if topic_id_int is not None:
        state = get_or_rebuild_state(topic_id_int, channel)
        if state is not None:
            cache_workers = state.get("workers") or {}

    presence = _get_presence(channel)
    detected = detect_crash_inconsistencies(
        cache_workers,
        reconstructed,
        presence,
        pending_spawn_stalled_threshold_min=pending_spawn_stalled_threshold_min,
        now=now,
    )

    applied: dict = {"cache_updates": [], "pings_sent": []}

    if not dry_run:
        auto_update_targets = list(detected["ghost_active"]) + [
            p for p in detected["pending_spawn"] if p.get("suggested_status")
        ]
        # cache.workers[alias].task_status を suggested_status に書き換えるため
        # cache JSON を直接 mutate して save_state する。projector wire-in が
        # 完成すれば本ブロックは不要になる (D#2750)。
        cache_state: OwState | None = None
        if auto_update_targets and topic_id_int is not None:
            cache_state = get_or_rebuild_state(topic_id_int, channel)
        for ghost in auto_update_targets:
            alias = ghost.get("alias")
            new_status = ghost.get("suggested_status")
            if not alias or not new_status:
                continue
            if cache_state is None:
                warnings.append(
                    f"failed to update cache for {ghost.get('task')} ({alias}): cache unavailable"
                )
                continue
            workers = cache_state.get("workers") or {}
            entry = workers.get(alias)
            if entry is None:
                # ghost_active は cache.workers 由来のため entry=None は race condition を示す
                # (detect 後に cache が再ロードされた等)。pending_spawn は projector 未配線で
                # entry が存在しないケースもある (D#2750)。いずれも warnings に記録して追跡可能にする。
                warnings.append(
                    f"cache entry missing for {ghost.get('task')} ({alias}) at update time "
                    "(possible race between detect_crash_inconsistencies and cache reload, "
                    "or projector not yet wired)"
                )
                continue
            previous = entry.get("task_status")
            entry["task_status"] = new_status
            workers[alias] = entry
            cache_state["workers"] = workers
            applied["cache_updates"].append(
                {
                    "task": ghost.get("task", ""),
                    "alias": alias,
                    "from": previous,
                    "to": new_status,
                }
            )
        if cache_state is not None and applied["cache_updates"] and topic_id_int is not None:
            try:
                save_state(topic_id_int, cache_state)
            except OSError as e:
                warnings.append(
                    f"failed to persist cache updates for topic {topic_id_str}: {e}"
                )

        for orphan in detected["orphans"]:
            result = _send_recovery_ping(channel, orphan["alias"])
            applied["pings_sent"].append(
                {
                    "alias": orphan["alias"],
                    "task": "T0",
                    "nonce": result.get("nonce"),
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
                    "nonce": result.get("nonce"),
                    "reason": "stalled_done",
                    "result": result,
                }
            )

    # pending_pings が渡された場合、nonce echo の有無を確認する
    nonce_echo_results: list[dict] = []
    for pp in pending_pings or []:
        echo_found = _check_nonce_echo(
            channel=channel,
            nonce=pp["nonce"],
            after_msg_id=pp.get("sent_after_msg_id", 0),
        )
        nonce_echo_results.append(
            {
                "alias": pp["alias"],
                "task": pp["task"],
                "nonce": pp["nonce"],
                "echo_found": echo_found,
            }
        )

    return {
        "detected": detected,
        "applied": applied,
        "warnings": warnings,
        "presence": presence,
        "reconstructed_max_msg_id": reconstructed.get("max_msg_id", 0),
        "dry_run": dry_run,
        "nonce_echo_results": nonce_echo_results,
    }


# ----------------------------
# identity bundle ヘルパー: term_ref（端末・セッション安定 ID）
# ----------------------------
#
# term_ref は worker セッションが住む物理単位（tmux pane 等）の安定 ID。
# SessionStart hook（hooks/term_ref_cache.py）が env キャッシュとして配置し、
# _maybe_inject_term_ref() が ow_send 時に event:identity.data.term_ref として自動補完する。
# reducer（ow_get_identity / ow_list_identities）は dict(data) で透過的に保持するため、
# reducer 側に追加ロジックは不要。本ヘルパーは「観測値の形式分類」と「妥当性判定」を
# 提供する純関数で、診断・ow_recover 用途に利用する。
#
# 認める形式:
#   - tmux:    "%N"                  例: "%5", "%123"     (tmux pane_id 規約)
#   - manual:  "manual:host:pid"      例: "manual:mac-mini:12345"

_TERM_REF_PATTERNS: dict[str, re.Pattern[str]] = {
    "tmux": re.compile(r"^%\d+$"),
    "manual": re.compile(r"^manual:[^:\s]+:\d+$"),
}


def classify_term_ref(value: object) -> str | None:
    """term_ref 値の形式を分類して種別名（"tmux"/"manual"）を返す。

    値が文字列でない、空文字、未知形式のいずれかなら None を返す。
    形式チェックは _TERM_REF_PATTERNS の定義順（tmux→manual）で先勝ち。
    """
    if not isinstance(value, str) or not value:
        return None
    for name, pattern in _TERM_REF_PATTERNS.items():
        if pattern.match(value):
            return name
    return None


def is_valid_term_ref(value: object) -> bool:
    """term_ref 値が認められた形式のいずれかに合致するかを判定する。"""
    return classify_term_ref(value) is not None


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


# ----------------------------
# reducer cache fastpath
# ----------------------------
#
# `_query_latest_event` / `_latest_events_by_type` は projector
# (`project_state_to_cache`) が書き出した OwState を読むだけのキャッシュ参照。
# キャッシュは orch tick で `project_state_to_cache` 経由に更新される前提で、
# reducer 自身は relay を直接叩かない (reducer は読むだけ、書き手は orch)。
#
# 対象 data_type: "identity" / "state" / "heartbeat" の3種。
# それ以外の data_type で呼ばれた場合は ``None`` / ``{}`` を返す
# (現状の呼び出し元はすべて上記3種、`_query_events_since` は対象外で従来通り)。


_CACHE_EVENT_FIELDS = ("identity_events", "states", "heartbeats")
_DATA_TYPE_TO_CACHE_FIELD: dict[str, str] = {
    "identity": "identity_events",
    "state": "states",
    "heartbeat": "heartbeats",
}


def _load_state_by_channel(channel: str) -> OwState | None:
    """channel から OwState をキャッシュ越しに読み出す。

    cache ディレクトリを ``find_topic_id_by_channel`` で走査して topic_id を
    特定し、``load_state(topic_id, channel=channel)`` で読み出す。topic_id が
    見つからない、または cache が無効なら ``None`` を返す (relay を叩かない)。

    TODO(perf): 現状 ``find_topic_id_by_channel`` と ``load_state`` で同一の
    cache JSON を二重に読んでいる (検索フェーズと検証フェーズ)。reducer の
    ホットパスで効くため、将来的に find_topic_id_by_channel が (topic_id, data)
    のタプルを返すよう拡張するか、_load_state_by_channel 内で iterdir を直接
    走査して 1 read で済ませるリファクタが望ましい。
    """
    topic_id = find_topic_id_by_channel(channel)
    if topic_id is None:
        return None
    return load_state(topic_id, channel=channel)


def _entry_to_parsed_event(entry: dict, handle: str, data_type: str) -> dict:
    """OwState の EventEntry を `_query_*` 戻り値形式 (parsed event) に組み立てる。

    既存呼び出し元 (ow_get_identity / ow_get_presence / ow_get_workload_state) の
    parsed event 取り扱いと互換にするため、kind=event / from=handle / data 同梱の
    envelope に組み戻す。
    """
    data = dict(entry.get("data") or {})
    if data.get("type") is None:
        data["type"] = data_type
    return {
        "msg_id": entry.get("msg_id", 0),
        "handle": handle,
        "body": {
            "v": 1,
            "kind": "event",
            "from": handle,
            "data": data,
        },
        "created_at": entry.get("created_at", ""),
    }


def _query_latest_event(
    channel: str, handle: str | None, data_type: str, since: int = 0
) -> dict | None:
    """指定 channel/handle/data_type の最新 event をキャッシュから返す。

    Args:
        channel: channelコード
        handle: workerハンドル（Noneなら全handle対象）
        data_type: eventのdata.type（"identity" / "state" / "heartbeat"）
        since: このmsg_idより大きいものを返す（0=全件）

    Returns:
        最新の parsed event dict（OwState の EventEntry から再構築）
        キャッシュ無し / 未サポート type / 該当 entry 無しなら None
    """
    field = _DATA_TYPE_TO_CACHE_FIELD.get(data_type)
    if field is None:
        return None
    state = _load_state_by_channel(channel)
    if state is None:
        return None
    events_map = state.get(field) or {}
    best: dict | None = None
    for h, entry in events_map.items():
        if not isinstance(entry, dict):
            continue
        if handle is not None and h != handle:
            continue
        msg_id = entry.get("msg_id", 0)
        if not isinstance(msg_id, int):
            continue
        if since and msg_id <= since:
            continue
        if best is None or msg_id > best["msg_id"]:
            best = _entry_to_parsed_event(entry, h, data_type)
    return best


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
    """指定 handle の各 data_type について最新 event をキャッシュから返す。

    Args:
        channel: channelコード
        handle: workerハンドル（Noneなら全handle対象）
        data_types: 対象とする event の data.type タプル
            ("identity" / "state" / "heartbeat" のみ対応、それ以外はキー欠落)

    Returns:
        {data_type: latest_event_dict} — 該当 event が無い type はキー欠落
    """
    state = _load_state_by_channel(channel)
    if state is None:
        return {}
    latest: dict[str, dict] = {}
    for data_type in data_types:
        field = _DATA_TYPE_TO_CACHE_FIELD.get(data_type)
        if field is None:
            continue
        events_map = state.get(field) or {}
        best: dict | None = None
        for h, entry in events_map.items():
            if not isinstance(entry, dict):
                continue
            if handle is not None and h != handle:
                continue
            msg_id = entry.get("msg_id", 0)
            if not isinstance(msg_id, int):
                continue
            if best is None or msg_id > best["msg_id"]:
                best = _entry_to_parsed_event(entry, h, data_type)
        if best is not None:
            latest[data_type] = best
    return latest


def ow_get_identity(channel: str, handle: str) -> dict | None:
    """指定 handle の最新 identity bundle を返す。crash 推論を含む。

    crash 推論: 最新の event:state が terminal でない（loading/ready/working/blocked/
               escalated/draining）かつ最後の event:heartbeat 受信時刻から閾値超過 →
               メモリ上で inferred_cause を付与（DB 不変）。

    term_ref 透過保持: event:identity.data に term_ref が含まれていれば dict(data) で
                      そのまま戻り値に乗る（reducer 側の加工なし）。形式判定が必要なら
                      is_valid_term_ref() / classify_term_ref() を別途呼び出す。

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

    term_ref 透過保持: ow_get_identity と同様、entry = dict(data) により term_ref も
                      そのまま保持される。

    実装: relay full pull は撤去し、``project_state_to_cache`` が書き出した
    OwState (``identity_events`` / ``states`` / ``heartbeats``) を読むだけ。
    キャッシュ未生成なら空リストを返す (orch 側で project_state_to_cache 必須)。
    """
    state = _load_state_by_channel(channel)
    if state is None:
        return []

    identity_by_handle: dict[str, dict] = state.get("identity_events") or {}
    state_by_handle: dict[str, dict] = state.get("states") or {}
    heartbeat_by_handle: dict[str, dict] = state.get("heartbeats") or {}

    entries = []
    for h, identity_entry in identity_by_handle.items():
        if not isinstance(identity_entry, dict):
            continue
        data = identity_entry.get("data") or {}
        entry = dict(data)
        entry["msg_id"] = identity_entry.get("msg_id", 0)
        entry["identity_at"] = identity_entry.get("created_at", "")

        state_entry = state_by_handle.get(h)
        workload_state = (
            (state_entry.get("data") or {}).get("state")
            if isinstance(state_entry, dict)
            else None
        )
        hb_entry = heartbeat_by_handle.get(h)
        last_heartbeat_at = (
            hb_entry.get("created_at")
            if isinstance(hb_entry, dict)
            else None
        )
        inferred_cause = _infer_crash_cause(workload_state, last_heartbeat_at)
        if inferred_cause is not None:
            entry["inferred_cause"] = inferred_cause

        if alive_only:
            cause = entry.get("cause")
            if cause in _TERMINATED_IDENTITY_CAUSES or entry.get("terminated_at"):
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
