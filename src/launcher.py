"""stdio <-> HTTP ブリッジ + デーモン起動ランチャー

Claude Code が stdio プロトコルで接続してくるエントリーポイント。
HTTPサーバーが未起動なら自動でデーモン起動し、
stdinからのJSON-RPCメッセージをStreamable HTTP経由で転送する。
サーバー側切断時は自動再接続を試み、stdin EOF時にセッション解除を行う。
"""
import asyncio
import atexit
import contextlib
import itertools
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from src.env_compat import env_get, env_set
from src.infra.detached_process import popen_detached
from src.infra.git_repo import resolve_main_repo_root
from src.infra.loopback_http import NO_PROXY_OPENER
from src.infra.session_identity import (
    HARNESS_CLAUDE_CODE,
    HARNESS_CODEX,
    ancestor_pids,
    detect_harness_by_ancestry,
    register_launcher_session,
    unregister_launcher_session,
)

logger = logging.getLogger(__name__)


def _propagate_plugin_root_env() -> None:
    """プラグイン実行時、embedding_service向けの `CALM_PROJECT_ROOT` を自動設定する。

    `embedding_service._resolve_project_root()` は env var 未設定時
    `git rev-parse --git-common-dir` で project root を解決するが、プラグインは
    `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` のようなgit管理外の
    ディレクトリに展開されて実行されるため、そこでは解決に失敗しRuntimeErrorになる
    （worktree誤解決によるメモリ膨張事故の再発防止のため、失敗時に`__file__`へ
    黙ってフォールバックしない設計自体は変更しない）。

    Claude Codeはプラグイン実行時、実行中のプラグインの展開先ディレクトリを
    `CLAUDE_PLUGIN_ROOT` として本プロセスに渡す。launcherはこのプロセスの
    子として `src.main` を、`src.main` はさらにその子として embedding_server を
    Popen（env指定なし=環境を継承）で起動するため、ここで設定しておけば
    プロセスツリー全体に伝播する。

    優先順位:
      1. `CALM_PROJECT_ROOT` が（新旧名いずれかで）既に明示設定されている場合は
         尊重し、上書きしない。
      2. `CLAUDE_PLUGIN_ROOT` が設定されていればその値を使う。
      3. どちらも無い場合、`os.getcwd()` を `resolve_main_repo_root()` に通した
         結果を設定する。`CLAUDE_PLUGIN_ROOT` はClaude Code本体側の間欠的な
         バグにより渡らないことがあるが、このプロセスは常に
         `uv run --directory <root> ...` 形式で起動されるため cwd は正しい
         ルートを指している。gitリポジトリ配下（worktree含む）では
         `resolve_main_repo_root()` のgit解決が先に成功するため、このフォール
         バックは実質的にgitではない配置（プラグインキャッシュ）でのみ意味を持つ。
    """
    if env_get("CALM_PROJECT_ROOT"):
        return
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        env_set("CALM_PROJECT_ROOT", plugin_root)
        return
    env_set("CALM_PROJECT_ROOT", str(resolve_main_repo_root(Path(os.getcwd()))))


def _read_max_retries() -> int | None:
    """env `CALM_LAUNCHER_MAX_RETRIES` からリトライ上限を読む。

    未設定・無効値・負値の場合は None（無限リトライ）を返す。
    D#2485 に基づき、HTTPサーバー復旧待ち継続のためデフォルトは無限。

    モジュールロード時にも呼ばれるため、警告は `logging.basicConfig` 未設定でも
    意図通り stderr に出るよう `print` で直接出す。
    """
    raw = env_get("CALM_LAUNCHER_MAX_RETRIES")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        print(
            f"[launcher] WARNING Invalid CALM_LAUNCHER_MAX_RETRIES={raw!r}, "
            "falling back to infinite",
            file=sys.stderr,
        )
        return None
    if value < 0:
        print(
            f"[launcher] WARNING CALM_LAUNCHER_MAX_RETRIES must be >= 0, "
            f"got {value}, falling back to infinite",
            file=sys.stderr,
        )
        return None
    return value


# リトライ設定（None = 無限。D#2485）
MAX_RETRIES: int | None = _read_max_retries()

# backoff 上限（秒）。指数的に伸びる sleep を一定でキャップし、
# 長時間のHTTPサーバー復旧待ちでもリトライ間隔を上限内に抑える。
BACKOFF_CAP_SEC = 60

# bridge identity ヘッダ名。全MCPリクエストに付与し、calm server 再起動を
# またいで安定な呼び出し元識別子として src/infra/session_identity.py が読む。
BRIDGE_SESSION_HEADER = "X-Calm-Bridge-Session-Id"

HEARTBEAT_INTERVAL_ENV = "CALM_LAUNCHER_HEARTBEAT_SEC"
DEFAULT_HEARTBEAT_INTERVAL_SEC = 60.0


def _read_heartbeat_interval_sec() -> float:
    """env `CALM_LAUNCHER_HEARTBEAT_SEC` から heartbeat 間隔を読む。

    未設定・無効値・0以下の場合は既定値にフォールバックする。
    """
    raw = env_get(HEARTBEAT_INTERVAL_ENV)
    if raw is None or raw == "":
        return DEFAULT_HEARTBEAT_INTERVAL_SEC
    try:
        value = float(raw)
    except ValueError:
        print(
            f"[launcher] WARNING Invalid {HEARTBEAT_INTERVAL_ENV}={raw!r}, "
            f"falling back to default {DEFAULT_HEARTBEAT_INTERVAL_SEC}s",
            file=sys.stderr,
        )
        return DEFAULT_HEARTBEAT_INTERVAL_SEC
    if value <= 0:
        print(
            f"[launcher] WARNING {HEARTBEAT_INTERVAL_ENV} must be > 0, "
            f"got {value}, falling back to default {DEFAULT_HEARTBEAT_INTERVAL_SEC}s",
            file=sys.stderr,
        )
        return DEFAULT_HEARTBEAT_INTERVAL_SEC
    return value


HEARTBEAT_INTERVAL_SEC = _read_heartbeat_interval_sec()

# stdin EOF後、server_to_stdoutの自然終了（read_streamのクローズ検知）を待つ
# 猶予秒数。超過したらtask group全体をキャンセルして強制退場する。
# stdin EOF = Claude Code終了であり、サーバー側が応答しない限り待ち続ける理由が
# ない（M#725「MCPブリッジハング調査」の沈黙ゾンビ化仮説への対策）。
STDIN_EOF_GRACE_SEC_ENV = "CALM_LAUNCHER_STDIN_EOF_GRACE_SEC"
DEFAULT_STDIN_EOF_GRACE_SEC = 10.0


def _read_stdin_eof_grace_sec() -> float:
    """env `CALM_LAUNCHER_STDIN_EOF_GRACE_SEC` から grace 秒数を読む。

    未設定・無効値・0以下の場合は既定値にフォールバックする。
    """
    raw = env_get(STDIN_EOF_GRACE_SEC_ENV)
    if raw is None or raw == "":
        return DEFAULT_STDIN_EOF_GRACE_SEC
    try:
        value = float(raw)
    except ValueError:
        print(
            f"[launcher] WARNING Invalid {STDIN_EOF_GRACE_SEC_ENV}={raw!r}, "
            f"falling back to default {DEFAULT_STDIN_EOF_GRACE_SEC}s",
            file=sys.stderr,
        )
        return DEFAULT_STDIN_EOF_GRACE_SEC
    if value <= 0:
        print(
            f"[launcher] WARNING {STDIN_EOF_GRACE_SEC_ENV} must be > 0, "
            f"got {value}, falling back to default {DEFAULT_STDIN_EOF_GRACE_SEC}s",
            file=sys.stderr,
        )
        return DEFAULT_STDIN_EOF_GRACE_SEC
    return value


STDIN_EOF_GRACE_SEC = _read_stdin_eof_grace_sec()

# server_to_stdoutがread_streamから例外オブジェクトを連続して受け取った場合の
# 上限回数。超過したらストリームが実質的に沈黙していると判断しループを打ち切り、
# ServerDisconnectedとして外側のリトライに接続する（M#725対策）。
MAX_CONSECUTIVE_STREAM_EXCEPTIONS_ENV = "CALM_LAUNCHER_MAX_CONSECUTIVE_STREAM_EXCEPTIONS"
DEFAULT_MAX_CONSECUTIVE_STREAM_EXCEPTIONS = 5


def _read_max_consecutive_stream_exceptions() -> int:
    """env `CALM_LAUNCHER_MAX_CONSECUTIVE_STREAM_EXCEPTIONS` から上限回数を読む。

    未設定・無効値・0以下の場合は既定値にフォールバックする。
    """
    raw = env_get(MAX_CONSECUTIVE_STREAM_EXCEPTIONS_ENV)
    if raw is None or raw == "":
        return DEFAULT_MAX_CONSECUTIVE_STREAM_EXCEPTIONS
    try:
        value = int(raw)
    except ValueError:
        print(
            f"[launcher] WARNING Invalid {MAX_CONSECUTIVE_STREAM_EXCEPTIONS_ENV}={raw!r}, "
            f"falling back to default {DEFAULT_MAX_CONSECUTIVE_STREAM_EXCEPTIONS}",
            file=sys.stderr,
        )
        return DEFAULT_MAX_CONSECUTIVE_STREAM_EXCEPTIONS
    if value <= 0:
        print(
            f"[launcher] WARNING {MAX_CONSECUTIVE_STREAM_EXCEPTIONS_ENV} must be > 0, "
            f"got {value}, falling back to default {DEFAULT_MAX_CONSECUTIVE_STREAM_EXCEPTIONS}",
            file=sys.stderr,
        )
        return DEFAULT_MAX_CONSECUTIVE_STREAM_EXCEPTIONS
    return value


MAX_CONSECUTIVE_STREAM_EXCEPTIONS = _read_max_consecutive_stream_exceptions()


class ServerDisconnected(Exception):
    """サーバー側の切断を示す例外。stdin EOFとの区別に使用する。"""
    pass


# サーバー接続設定
# CALM_URL が設定されていればそのURLを使い、未設定ならローカルHTTPサーバーに接続する。
# リモートURL指定時はサーバー自動起動・セッション管理をスキップする。
_REMOTE_URL = env_get("CALM_URL")

if _REMOTE_URL:
    if not _REMOTE_URL.startswith(("http://", "https://")):
        raise ValueError(
            f"CALM_URL must start with http:// or https://, got: {_REMOTE_URL!r}"
        )
    _base = _REMOTE_URL.rstrip("/")
    MCP_ENDPOINT = f"{_base}/mcp"
    _IS_LOCAL = False
else:
    from src.http_config import HTTP_HOST, HTTP_PORT
    _base = f"http://{HTTP_HOST}:{HTTP_PORT}"
    MCP_ENDPOINT = f"{_base}/mcp"
    _IS_LOCAL = True

SESSION_REGISTER_URL = f"{_base}/session/register"
SESSION_UNREGISTER_URL = f"{_base}/session/unregister"

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

# セッションID（プロセスごとにユニーク）
_session_id = str(uuid.uuid4())

# クリーンアップ状態
_cleanup_done = False


# =============================================
# デーモン起動ロジック（embedding_serviceパターン踏襲）
# =============================================


def _open_bridge_request(req: urllib.request.Request, *, timeout: float):
    """ローカルモード(ループバック接続)はプロキシを常に無視し、リモートモードは
    既定のプロキシ設定(環境変数・Windowsレジストリ等)に従う。

    リモートモードの接続先はユーザーが指定した任意のホストであり、社内プロキシ
    経由が必要な場合があるため、ループバック専用のNO_PROXY_OPENERを使わない。
    """
    if _IS_LOCAL:
        return NO_PROXY_OPENER.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _is_server_running() -> bool:
    """HTTPサーバーの生存確認を行う。

    MCP Streamable HTTP の POST /mcp にアクセスしてステータスコードで判定する。
    405 (Method Not Allowed for GET) も「起動済み」と見なす。
    """
    try:
        req = urllib.request.Request(
            MCP_ENDPOINT,
            method="GET",
        )
        with _open_bridge_request(req, timeout=2) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        # 4xx系HTTPエラーは「サーバー起動済み」を意味する
        return e.code in (405, 406, 400)
    except Exception:
        return False


def _server_stderr_log_path() -> Path:
    """起動直後に落ちたサーバーの手がかりを残すstderrログ先。

    `_setup_server_logging`（src/main.py）がserver.logを置く`logs/`
    ディレクトリと同じ場所に置く。import時点で落ちるような致命的な失敗
    （トップレベルimportの例外等）はlogging設定前のため、そちらでは
    拾えずこちらにしか残らない。
    """
    from src.db import get_db_path

    return Path(get_db_path()).parent / "logs" / "server.stderr.log"


@contextlib.contextmanager
def _resolve_server_stderr_target():
    """server.stderr.logを開いて渡す。準備に失敗したらDEVNULLにフォールバックする。

    診断用ログの用意（ディレクトリ作成・ファイルオープン）自体の失敗は、
    サーバー起動そのものを止める理由にしない。
    """
    try:
        stderr_path = _server_stderr_log_path()
        stderr_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stderr_log = open(stderr_path, "wb")
    except OSError as e:
        logger.warning(f"Failed to prepare server stderr log, falling back to DEVNULL: {e}")
        yield subprocess.DEVNULL
        return
    try:
        yield stderr_log
    finally:
        stderr_log.close()


def _start_http_server() -> bool:
    """HTTPサーバーをデーモンとして起動する。

    sys.executableは.mcp.jsonの「uv run python -m src.launcher」経由で
    起動されることを前提とし、uv仮想環境のPython（.venv/bin/python）を使用する。
    stderrは可能ならDEVNULLではなくファイルへ向ける（肥大しないよう起動のたびに
    上書きする。蓄積した過去ログが必要になるケースは想定していない）。
    """
    try:
        with _resolve_server_stderr_target() as stderr_target:
            popen_detached(
                [sys.executable, "-m", "src.main", "--transport", "http"],
                stdout=subprocess.DEVNULL,
                stderr=stderr_target,
                cwd=_PROJECT_ROOT,
            )
    except OSError as e:
        logger.warning(f"Failed to start HTTP server: {e}")
        return False
    logger.info("HTTP server process started")
    return True


def _ensure_server_running() -> bool:
    """ヘルスチェック -> 起動 -> 待機のフロー。成功でTrue、タイムアウトでFalse。"""
    if _is_server_running():
        return True
    # ロックファイルが存在する場合、別のランチャーが起動中の可能性がある。
    # 二重起動を避けてサーバーの準備完了を待つだけにする。
    # ただしプロセスが死んでいる、またはPID再利用で別プロセスに入れ替わって
    # いる場合はstale lockとして削除し、新規起動する（ポートの生死はここでは
    # 見ない。acquire()とmcp.run()の間にポート未listenの窓があり、ここで
    # ポートを見ると起動直後の正常なサーバーをstale誤判定しうるため）。
    from src.infra.lock_file import read as read_lock, is_lock_stale
    from src.infra.lock_file import LOCK_FILE

    lock_info = read_lock()
    if lock_info is not None and is_lock_stale(lock_info):
        logger.info(f"Removing stale lock file: pid={lock_info['pid']}")
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"Failed to remove stale lock file: {e}")
        lock_info = None
    if lock_info is None:
        if not _start_http_server():
            return False
    # 最大30秒待機（0.5秒間隔 x 60回）
    for _ in range(60):
        time.sleep(0.5)
        if _is_server_running():
            logger.info("HTTP server is ready")
            return True
    logger.warning("HTTP server failed to start within 30 seconds")
    return False


# =============================================
# セッションライフサイクル管理
# =============================================


# _current_harness_name() の判定結果。祖先 pid の探索は `ps` を複数回呼ぶため、
# heartbeat ごとの再登録で繰り返さないよう初回の結果を保持する（launcher の
# 生存中に起動元 CLI が変わることは無い）。
_harness_name_cache: str | None = None


def _current_harness_name() -> str:
    """launcherを起動したエージェントCLIの種別名を判定する。

    判定順:
    1. launcherプロセス自身の`CALM_HARNESS`が`codex`ならcodex
       (src.harness.select_harnessと同じ明示指定)
    2. 祖先プロセスを自分に近い順にたどり、最初に見つかったエージェントCLI
       (detect_harness_by_ancestry)
    3. どちらでも決まらなければclaude_code(従来挙動)

    2 が必要なのは、Codexが`~/.codex/config.toml`の`[mcp_servers.calm]`から
    launcherを起動する際、MCPサーバーへ親の環境変数を引き継がず、
    `CALM_HARNESS`も付与されないため。この状態で既定のclaude_codeへ倒すと、
    Claude CodeのBashツールから起動した`codex exec`配下のlauncherが、親の
    Claude Codeセッションとして台帳へ記録される。近い側のCLIを採用すれば
    入れ子構成でもCodexと判定できる。

    calm server は launcher から見て別プロセス(ローカルは launcher が
    subprocess.Popen で起動する子、リモードは既存の常駐プロセス)で、複数の
    launcher(異なるharness由来を含む)を1つのserverプロセスが共有しうるため、
    server側の自プロセスではなくlauncher側で判定してPOSTボディに乗せる。
    """
    global _harness_name_cache
    if _harness_name_cache is not None:
        return _harness_name_cache
    if env_get("CALM_HARNESS", "").lower() == HARNESS_CODEX:
        name = HARNESS_CODEX
    else:
        name = (
            detect_harness_by_ancestry(ancestor_pids(os.getpid()))
            or HARNESS_CLAUDE_CODE
        )
    _harness_name_cache = name
    return name


def _register_session() -> bool:
    """セッション登録（POST /session/register）

    harness/hostはセッション台帳(sessionsテーブル)向けの申告値。launcher自身の
    envと実行ホストでしか判定できない値のため、ここでPOSTボディに含める。
    """
    try:
        data = json.dumps({
            "session_id": _session_id,
            "harness": _current_harness_name(),
            "host": socket.gethostname(),
        }).encode("utf-8")
        req = urllib.request.Request(
            SESSION_REGISTER_URL,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with _open_bridge_request(req, timeout=5) as resp:
            result = json.loads(resp.read())
            logger.info(f"Session registered: {result}")
            return True
    except Exception as e:
        logger.warning(f"Session register failed: {e}")
        return False


def _unregister_session() -> bool:
    """セッション解除（POST /session/unregister）"""
    try:
        data = json.dumps({"session_id": _session_id}).encode("utf-8")
        req = urllib.request.Request(
            SESSION_UNREGISTER_URL,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with _open_bridge_request(req, timeout=5) as resp:
            result = json.loads(resp.read())
            logger.info(f"Session unregistered: {result}")
            return True
    except Exception as e:
        logger.warning(f"Session unregister failed: {e}")
        return False


def _cleanup():
    """セッション解除 + 登録ファイル削除 + ログ出力"""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    _unregister_session()
    unregister_launcher_session()


# =============================================
# stdio <-> HTTP ブリッジ
# =============================================


class _StdinBridgeState:
    """stdin読み取りとサーバー転送キューの、bridgeリトライを跨いだ永続状態。

    `_run_retry_loop` 内で1回だけ生成し、`_stdin_reader_task`（プロセス生存中
    ずっと動く）と複数回呼ばれる`_bridge`（bridge失敗のたびに再実行される）の
    双方から共有する。stdin側の読み取りバッファ・読み取りスレッドは
    `_stdin_reader_task`のローカル変数として閉じ込め、ここでは「サーバーへ
    転送待ちのメッセージ」「サーバーへ送信済みで応答待ちのリクエストid」
    「stdinがEOFに達したか」の3つだけを持つ。

    `outbound`は、stdin EOF時に`_stdin_reader_task`が番兵として`None`を積む。
    `None`を取り出した側（`_bridge`の`queue_to_server`）は、まだ動いている
    かもしれない次のbridge試行のために`None`を積み直してから抜ける。
    """

    def __init__(self) -> None:
        self.outbound: asyncio.Queue = asyncio.Queue()
        self.pending_ids: set = set()
        self.stdin_eof = asyncio.Event()


def _message_id(message):
    """JSONRPCMessageのroot（Request/Notification/Response/Error）からidを取り出す。

    Notificationにはid自体が存在しないため、その場合はNoneを返す。
    """
    return getattr(message.root, "id", None)


def _write_message_to_stdout(message) -> None:
    """JSONRPCMessageを1行のJSONとしてstdoutに書く。"""
    json_bytes = message.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
    sys.stdout.buffer.write(json_bytes + b"\n")
    sys.stdout.buffer.flush()


def _write_jsonrpc_error(request_id, code: int, message: str) -> None:
    """サーバーへ転送せず、launcher自身がstdoutへJSON-RPCエラー応答を書く。"""
    from mcp import types

    _write_message_to_stdout(
        types.JSONRPCMessage(
            root=types.JSONRPCError(
                jsonrpc="2.0",
                id=request_id,
                error=types.ErrorData(code=code, message=message),
            )
        )
    )


def _fail_pending_requests(state: "_StdinBridgeState", message: str) -> None:
    """bridge失敗時、まだ応答の無いin-flightリクエストidそれぞれにエラーを返す。

    通知（idの無いメッセージ）はそもそも`pending_ids`に載らないため対象外。
    """
    for request_id in list(state.pending_ids):
        _write_jsonrpc_error(request_id, -32603, message)
    state.pending_ids.clear()


def _handle_stdin_line(line: bytes, state: "_StdinBridgeState") -> None:
    """stdinの1行をJSON-RPCメッセージとしてパースし、振り分ける。

    Claude Codeはinitializeより前にバージョン交渉probe（`server/discover`）を
    送ってくることがある。MCP仕様上、このprobeにはどんなエラー応答を返しても
    （401/403以外）clientは従来のinitializeにフォールバックするため、サーバーへ
    転送せずlauncher自身が即座にMethod not foundで応答する。これによりHTTP
    サーバーが未起動・再接続中でも応答できる。discoverが通知（idなし）で
    来た場合は応答せず捨てる。
    """
    from mcp import types
    from mcp.shared.message import SessionMessage

    try:
        message = types.JSONRPCMessage.model_validate_json(line)
    except Exception:
        logger.exception("Failed to parse stdin message")
        return

    if getattr(message.root, "method", None) == "server/discover":
        request_id = _message_id(message)
        if request_id is not None:
            _write_jsonrpc_error(request_id, -32601, "Method not found")
        return

    state.outbound.put_nowait(SessionMessage(message))


_ERROR_BROKEN_PIPE = 109
_FILE_TYPE_PIPE = 3

# PeekNamedPipeで読める量が0の間、再確認までの待ち時間（秒）。stdin到着から
# launcherが気づくまでの応答遅延の上限になる。
_WINDOWS_PIPE_POLL_INTERVAL_SEC = 0.02


class _PeekNamedPipeFailed(OSError):
    """PeekNamedPipeが失敗したことを示す。winerrorにGetLastErrorの値を持つ。"""

    def __init__(self, winerror: int) -> None:
        super().__init__(f"PeekNamedPipe failed with winerror={winerror}")
        self.winerror = winerror


def _win_stdin_pipe_api(fd: int):
    """Windows専用: stdin(fd)のGetFileTypeとPeekNamedPipeの薄いラッパーを返す。

    `msvcrt`・`ctypes.WinDLL`・`ctypes.get_last_error()`はWindows以外には
    存在しないため、呼び出し側（Windowsでのみ到達する経路）の中でだけ
    importする。共有の`ctypes.windll.kernel32`にargtypesを設定すると
    プロセス全体に影響するため、専用の`WinDLL`を使う。
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    kernel32.GetFileType.restype = wintypes.DWORD
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL

    handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
    file_type = kernel32.GetFileType(handle)

    def peek() -> int:
        """ブロックせず読める残量(バイト数)を返す。失敗時は`_PeekNamedPipeFailed`。"""
        bytes_available = wintypes.DWORD(0)
        ok = kernel32.PeekNamedPipe(
            handle, None, 0, None, ctypes.byref(bytes_available), None
        )
        if not ok:
            raise _PeekNamedPipeFailed(ctypes.get_last_error())
        return bytes_available.value

    return file_type, peek


def _read_stdin_chunk_windows_pipe(fd: int, peek) -> bytes:
    """Windowsの無名パイプstdin専用の読み取り。空バイト列はEOFを意味する。

    別スレッドでのnumpy初回import（同梱OpenBLASのDLLロード）が、stdinの
    ブロッキング読み取り中は完了しない問題への対策。`peek()`で読める量を
    確認し、0の間はブロックせず待ってから再確認する。
    """
    while True:
        try:
            available = peek()
        except _PeekNamedPipeFailed as e:
            if e.winerror == _ERROR_BROKEN_PIPE:
                return b""  # 書き込み側close = EOF
            logger.warning(
                f"PeekNamedPipe failed (winerror={e.winerror}), treating as EOF"
            )
            return b""
        if available > 0:
            return os.read(fd, min(available, 65536))
        time.sleep(_WINDOWS_PIPE_POLL_INTERVAL_SEC)


def _stdin_chunk_reader(fd: int):
    """stdinの読み方を1チャンク分選ぶ。呼び出すたびに1チャンク返す関数を返す。

    Windowsでstdinが無名パイプ（GetFileType == FILE_TYPE_PIPE）の場合だけ
    PeekNamedPipe方式を使う。それ以外（POSIX全般、Windowsでファイル
    リダイレクト等パイプでない場合）は従来通りのブロッキング`os.read`。
    """
    if sys.platform == "win32":
        file_type, peek = _win_stdin_pipe_api(fd)
        if file_type == _FILE_TYPE_PIPE:
            return lambda: _read_stdin_chunk_windows_pipe(fd, peek)
    return lambda: os.read(fd, 65536)


async def _stdin_reader_task(state: "_StdinBridgeState") -> None:
    """stdinをプロセス生存中1回だけ読み取り続ける、bridgeのリトライを跨いで
    生きるタスク。

    bridgeが失敗して再接続を試みている間も、このタスク自体は止まらない。
    OSレベルの読み取りはブロッキングのdaemonスレッドで回し、
    `loop.call_soon_threadsafe`経由でイベントループ側の
    `asyncio.StreamReader`へ`feed_data`/`feed_eof`する（asyncioの既定
    イベントループの中には、stdinを`connect_read_pipe`で読もうとすると
    壊れるものがあるため、全OS共通でスレッド読み取りに揃える）。読み取り方
    自体は`_stdin_chunk_reader`が選ぶ（POSIXおよびWindowsでパイプでない
    場合はブロッキングの`os.read`、Windowsで無名パイプの場合はPeekNamedPipe
    方式）。イベントループが既に閉じた後にスレッド側から
    `call_soon_threadsafe`を呼ぶとRuntimeErrorになるが、daemonスレッド
    なのでプロセス終了は妨げず、その例外は握りつぶしてよい。読み取りの
    開始・継続いずれが失敗してもWARNINGを出したうえでEOF扱いにし、終了
    経路に必ず乗せる。
    行ごとにパースして`_handle_stdin_line`に渡し、stdin EOFで終了する。
    終了時は`state.outbound`へ番兵として`None`を積んでから
    `state.stdin_eof`をセットする（`queue_to_server`側が素の`Queue.get()`
    だけでEOFを検知できるようにするため。`asyncio.wait`でgetとEvent待ちを
    競わせる方式は、getが完了した直後にキャンセルされると取り出した
    メッセージが誰にも回収されずに消える窓があるため使わない）。
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()

    def _feed_data(chunk: bytes) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(reader.feed_data, chunk)

    def _feed_eof() -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(reader.feed_eof)

    def _read_stdin() -> None:
        try:
            fd = sys.stdin.buffer.fileno()
            read_chunk = _stdin_chunk_reader(fd)
            while True:
                chunk = read_chunk()
                if not chunk:
                    break
                _feed_data(chunk)
        except Exception:
            logger.warning("Failed to read stdin", exc_info=True)
        finally:
            _feed_eof()

    buffer = b""
    try:
        # スレッド生成自体の失敗（例: OSのスレッド数上限）もここでEOF終了
        # 経路に乗せる必要があるため、try の外ではなく内側で起動する。
        threading.Thread(target=_read_stdin, daemon=True).start()
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                _handle_stdin_line(line, state)
    except Exception:
        # _handle_stdin_line呼び出し先（stdoutへの書き込み等）の失敗も含め、
        # ここで吸収しないとこのタスクが例外で終わり、_run_retry_loopの
        # 後始末（CancelledErrorだけをsuppressする）をすり抜けてプロセスが
        # クラッシュする。
        logger.warning("stdin reader ended unexpectedly", exc_info=True)
    finally:
        if buffer.strip():
            logger.warning(
                f"Discarding {len(buffer)} bytes of incomplete data in stdin buffer"
            )
        state.outbound.put_nowait(None)
        state.stdin_eof.set()


async def _bridge(state: "_StdinBridgeState") -> None:
    """共有state経由でJSON-RPCメッセージをHTTP POST /mcpに転送し、レスポンスを
    stdoutに書く。

    MCP SDK の streamable_http_client を利用し、ストリーム間のブリッジを行う。
    stdinの読み取り自体は`_stdin_reader_task`が担い、ここでは`state.outbound`
    キューから取り出してサーバーへ送るだけ（bridge失敗のたびに毎回作り直される
    のはHTTP接続側であり、stdin側は作り直さない）。
    正常終了（stdin EOFの番兵を`state.outbound`から受け取った）時はreturn、
    サーバー側切断時はServerDisconnectedをraiseする。
    """
    # 遅延import: デーモン起動ロジックはMCP SDKに依存しないため、
    # ブリッジ実行時まで重いimportを遅延させて起動速度を確保する
    import anyio
    import httpx
    from mcp import types
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

    # stdin EOFとサーバー切断を区別するためのフラグ
    # stdin EOF: queue_to_serverが先に終了 → 正常終了
    # サーバー切断: server_to_stdoutが先に終了 → stdin_eofがFalse → ServerDisconnected
    stdin_eof = False

    # 全MCPリクエストに bridge identity ヘッダを同梱する。calm server が
    # 再起動しても launcher プロセス（＝ _session_id）が生きている限り不変な値で、
    # 呼び出し元セッション識別子の解決（src/infra/session_identity.py）が読む。
    #
    # create_mcp_http_clientはtrust_envを渡せないため、同等のデフォルト
    # timeoutを明示してhttpx.AsyncClientを直接組み立てる。ローカルモードでは
    # trust_env=False（環境のプロキシ設定を無視する）にする。ループバック
    # 接続が、手動プロキシ設定はあるが環境変数が無い環境で社内プロキシへ
    # 誤って送られるのを防ぐため（リモートモードはプロキシ経由が必要な
    # 場合があるため既定どおりtrust_env=Trueのままにする）。
    http_client = httpx.AsyncClient(
        headers={
            BRIDGE_SESSION_HEADER: _session_id,
        },
        timeout=httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
        trust_env=not _IS_LOCAL,
    )
    async with http_client:
        # terminate_on_close=True: 切断時に DELETE でMCPセッションを終了させる。
        # ブリッジは再接続時にセッションを再利用せず毎回新規に張るため、DELETE を
        # 送らないとサーバー側の StreamableHTTPSessionManager が旧セッション
        # （タスク+トランスポート）をサーバー停止まで保持し続けてメモリが単調増加する。
        # サーバー側切断が原因で閉じる場合の DELETE 失敗は SDK 内で握りつぶされる。
        async with streamable_http_client(
            url=MCP_ENDPOINT,
            http_client=http_client,
            terminate_on_close=True,
        ) as (read_stream, write_stream, _get_session_id):

            async def queue_to_server() -> None:
                """共有state.outboundキューから1件ずつ取り出し、write_streamに送る。

                取り出しは素の`await state.outbound.get()`のみで行う（キャンセルされても
                取り出し自体は起きない、というQueueの保証に乗るため）。番兵`None`を
                受け取ったらstdin EOFとして扱い、次のbridge試行のために積み直してから
                抜ける。クライアント→サーバーのリクエスト（idあり）だけを
                `state.pending_ids`に記録する。bridgeが失敗して転送できなかった分は、
                呼び出し側（リトライループ）が`_fail_pending_requests`でエラー応答に倒す。
                """
                nonlocal stdin_eof
                try:
                    while True:
                        session_msg = await state.outbound.get()
                        if session_msg is None:
                            stdin_eof = True
                            state.outbound.put_nowait(None)
                            break
                        message = session_msg.message
                        if isinstance(message.root, types.JSONRPCRequest):
                            state.pending_ids.add(message.root.id)
                        await write_stream.send(session_msg)
                finally:
                    await write_stream.aclose()

            async def server_to_stdout() -> None:
                """read_streamからメッセージを受信し、stdoutに書く。

                read_streamが終了したとき、stdin_eofがFalseならサーバー側切断と判断し
                ServerDisconnectedをraiseしてtask group全体をキャンセルする。
                読み取り中に例外オブジェクトを MAX_CONSECUTIVE_STREAM_EXCEPTIONS 回
                連続して受け取った場合は、ストリームが実質的に沈黙していると判断し
                ループを自発的に打ち切る（finally節が同様に ServerDisconnected へ倒す）。
                サーバー→クライアントの応答（Response/Error）のidだけを
                `state.pending_ids`から取り除く。
                """
                consecutive_exceptions = 0
                try:
                    async for session_msg_or_exc in read_stream:
                        if isinstance(session_msg_or_exc, Exception):
                            consecutive_exceptions += 1
                            logger.warning(f"Received exception from server: {session_msg_or_exc}")
                            if consecutive_exceptions >= MAX_CONSECUTIVE_STREAM_EXCEPTIONS:
                                logger.warning(
                                    f"Giving up after {consecutive_exceptions} "
                                    "consecutive stream exceptions"
                                )
                                break
                            continue
                        consecutive_exceptions = 0
                        message = session_msg_or_exc.message
                        if isinstance(message.root, (types.JSONRPCResponse, types.JSONRPCError)):
                            state.pending_ids.discard(message.root.id)
                        _write_message_to_stdout(message)
                except anyio.ClosedResourceError:
                    pass
                except Exception:
                    logger.debug("stdout writer ended", exc_info=True)
                finally:
                    if not stdin_eof:
                        raise ServerDisconnected("Server connection lost")

            async def heartbeat_loop() -> None:
                """一定間隔で /session/register を再送し、SessionManager 側の
                last_seen を更新し続ける（session_manager.py の TTL 失効に対する
                生存申告）。登録エンドポイントを持たない接続先（例: セッションAPI
                を持たない remote 展開）でも _register_session() が例外を握り
                つぶして False を返すだけなので、この loop は次回間隔まで待って
                再試行するだけで致命化しない。

                このループ自体は自発的に終了しないため、io_tasks() 側から
                明示的にキャンセルされて初めて終了する。
                """
                while True:
                    await anyio.sleep(HEARTBEAT_INTERVAL_SEC)
                    await anyio.to_thread.run_sync(_register_session)

            async def io_tasks() -> None:
                """queue_to_server / server_to_stdout をまとめて実行する。

                この内側 task group が例外なく完了するのは stdin EOF による
                正常終了時のみ（サーバー切断時は server_to_stdout が
                ServerDisconnected を raise し、ここで捕まえずそのまま外側の
                tg へ伝播させて全タスクをキャンセルさせる）。
                正常終了時は heartbeat_loop が無限ループのため自発的に終わらず、
                外側 tg の cancel_scope を明示的にキャンセルして道連れにする。

                stdin EOF後、server_to_stdout が STDIN_EOF_GRACE_SEC 以内に自発終了
                しない場合（サーバー側がストリームを閉じない「沈黙ゾンビ化」）、
                eof_watchdog が io_tg 自体をキャンセルして強制退場する。
                この経路では stdin_eof が既に True のため、server_to_stdout の
                finally 節は ServerDisconnected を raise しない（意図的な正常終了）。
                """
                stdin_done = anyio.Event()
                stdout_done = anyio.Event()

                async def queue_to_server_wrapper() -> None:
                    try:
                        await queue_to_server()
                    finally:
                        stdin_done.set()

                async def server_to_stdout_wrapper() -> None:
                    try:
                        await server_to_stdout()
                    finally:
                        stdout_done.set()

                async def eof_watchdog() -> None:
                    await stdin_done.wait()
                    with anyio.move_on_after(STDIN_EOF_GRACE_SEC):
                        await stdout_done.wait()
                        return
                    logger.warning(
                        f"server_to_stdout did not finish within "
                        f"{STDIN_EOF_GRACE_SEC}s of stdin EOF; forcing bridge exit"
                    )
                    io_tg.cancel_scope.cancel()

                async with anyio.create_task_group() as io_tg:
                    io_tg.start_soon(queue_to_server_wrapper)
                    io_tg.start_soon(server_to_stdout_wrapper)
                    io_tg.start_soon(eof_watchdog)
                tg.cancel_scope.cancel()

            async with anyio.create_task_group() as tg:
                tg.start_soon(io_tasks)
                tg.start_soon(heartbeat_loop)


async def _run_retry_loop() -> None:
    """1つのイベントループ内でstdin読み取りとbridgeのリトライ全体を回す。

    stdinの読み取り（`_stdin_reader_task`、内部のdaemonスレッドを含む）は
    ここで1回だけ起動し、bridge（`_bridge`）が何度失敗しても作り直さない。
    読み取りスレッドは生成元の`asyncio`イベントループを閉じ込めて
    `call_soon_threadsafe`で戻すため、bridge のたびに`asyncio.run`をやり直すと
    スレッドが古い（既に閉じた）ループを参照したまま残ってしまう
    （作り直すべきなのはHTTP接続側だけ）。

    サーバー側切断時は自動でリトライする。MAX_RETRIES が None なら無限、
    数値指定なら最大 MAX_RETRIES 回。stdin EOF（Claude Code終了）時は即座に終了する。
    """
    state = _StdinBridgeState()
    reader_task = asyncio.ensure_future(_stdin_reader_task(state))
    try:
        max_retries = MAX_RETRIES
        retries_label = "inf" if max_retries is None else str(max_retries)

        for attempt in itertools.count():
            # stdin EOFが既に確定していれば、以降のサーバー起動待ち・登録・bridge
            # 再接続は一切行わずここで終了する（バックオフ中にEOFへ達した場合の
            # 次回入り口もここ）。
            if state.stdin_eof.is_set():
                break

            # 1. HTTPサーバーの起動確認（ローカルのみ。リモートはOAuth等の制約があるためスキップ）
            if _IS_LOCAL and not await asyncio.to_thread(_ensure_server_running):
                logger.error("Failed to ensure HTTP server is running")
                sys.exit(1)

            # 2. セッション登録（ローカル/リモート問わず試行する）。
            #    ローカルは登録失敗を致命エラーとして扱う（ローカルサーバーは常に
            #    このAPIを持つため、失敗は異常事態）。リモートは接続先がセッション
            #    APIを持たない場合があるため、警告ログのみで続行する（bridge identity
            #    ヘッダによる declaration/inbox 安定化自体はセッション登録の成否に
            #    依存しない。ただしこの場合 lease_loop の生存ゲート対象には含まれない）。
            registered = await asyncio.to_thread(_register_session)
            if _IS_LOCAL and not registered:
                logger.error("Failed to register session")
                sys.exit(1)
            if not _IS_LOCAL and not registered:
                logger.warning(
                    "Session register failed (destination may not support "
                    "the session API); continuing without liveness heartbeat"
                )

            # 3. stdio <-> HTTP ブリッジ起動
            try:
                await _bridge(state)
                break  # stdin EOF → 正常終了
            except Exception as e:
                # server_to_stdoutのfinally節は、外部からのキャンセル（SIGINTで
                # asyncio.runがmain taskをcancelする経路、SIGTERM後の後始末で
                # 全タスクをcancelする経路のいずれも）でstdin_eofがFalseのまま
                # 中断された場合もServerDisconnectedを送出する。これにより
                # CancelledError（BaseException）がここのexcept Exceptionで
                # 捕まる形のExceptionに化けてしまい、キャンセル要求を「ただの
                # bridge失敗」としてリトライし続けてしまう。自タスクが実際に
                # キャンセル中（cancelling() > 0）なら、化けた例外を元の
                # CancelledErrorに戻して外側へ伝播させ、リトライさせない。
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError() from e
                # anyioのExceptionGroupによりServerDisconnectedが直接キャッチできない
                # ケースがあるため、例外の種類を問わず統一的にリトライする。
                # このbridge実行中に送信済みで応答の無かったリクエストへは、
                # リトライの成否に関わらずここでエラー応答を返す。
                _fail_pending_requests(state, "CALM server connection lost")
                if max_retries is not None and attempt >= max_retries:
                    logger.error("Bridge failed, max retries (%d) exceeded: %s", max_retries, e)
                    break
                backoff = min(2 ** (attempt + 1), BACKOFF_CAP_SEC)
                logger.warning(
                    "Bridge failed (%s), retrying in %ds (%d/%s)",
                    e, backoff, attempt + 1, retries_label,
                )
                # backoff全体を待ち切らず、stdin EOFが先に来たら即座に抜ける
                # （そのままループ先頭のEOFチェックで終了する）。
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(state.stdin_eof.wait(), timeout=backoff)
    finally:
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader_task


def main() -> None:
    """ランチャーのメインエントリーポイント"""
    # ログ設定（stderrへ出力、stdoutはMCPプロトコル用）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [launcher] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    # プラグイン実行時、embedding_serviceのproject root解決に使うenv varを
    # 子プロセス（src.main、さらにその子のembedding_server）へ伝播させるため
    # 最初に設定する。
    _propagate_plugin_root_env()

    # セッション解除(atexit/SIGTERM)はローカル/リモード問わず常時登録する。
    # _unregister_session()は失敗を握りつぶすため、登録エンドポイントを持たない
    # 接続先（例: セッションAPIを持たないremote展開）でも安全に呼べる。
    atexit.register(_cleanup)
    _exit_handler = lambda *_: sys.exit(0)  # atexitが発火する
    signal.signal(signal.SIGTERM, _exit_handler)
    # SIGBREAK（Ctrl+Break）はWindowsにしか無い。
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _exit_handler)

    # SessionStart hook（Claude Code CLI プロセスの別の子孫）や、calm
    # server 側のセッション別名解決（src/infra/session_identity.py の
    # resolve_cli_session）が祖先 pid チェーン経由で自分を見つけられるよう、
    # HTTPサーバー起動待機（最大30秒）より前に登録ファイルを書く。
    # 書込失敗は非致命（ベストエフォート）。
    register_launcher_session(_session_id, harness=_current_harness_name())

    if not _IS_LOCAL:
        logger.info("Remote mode: connecting to %s", MCP_ENDPOINT)

    try:
        asyncio.run(_run_retry_loop())
    except KeyboardInterrupt:
        pass

    _cleanup()


if __name__ == "__main__":
    main()
