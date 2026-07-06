"""stdio <-> HTTP ブリッジ + デーモン起動ランチャー

Claude Code が stdio プロトコルで接続してくるエントリーポイント。
HTTPサーバーが未起動なら自動でデーモン起動し、
stdinからのJSON-RPCメッセージをStreamable HTTP経由で転送する。
サーバー側切断時は自動再接続を試み、stdin EOF時にセッション解除を行う。
"""
import asyncio
import atexit
import itertools
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


def _read_max_retries() -> int | None:
    """env `CC_MEMORY_LAUNCHER_MAX_RETRIES` からリトライ上限を読む。

    未設定・無効値・負値の場合は None（無限リトライ）を返す。
    D#2485 に基づき、HTTPサーバー復旧待ち継続のためデフォルトは無限。

    モジュールロード時にも呼ばれるため、警告は `logging.basicConfig` 未設定でも
    意図通り stderr に出るよう `print` で直接出す。
    """
    raw = os.environ.get("CC_MEMORY_LAUNCHER_MAX_RETRIES")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        print(
            f"[launcher] WARNING Invalid CC_MEMORY_LAUNCHER_MAX_RETRIES={raw!r}, "
            "falling back to infinite",
            file=sys.stderr,
        )
        return None
    if value < 0:
        print(
            f"[launcher] WARNING CC_MEMORY_LAUNCHER_MAX_RETRIES must be >= 0, "
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

# bridge identity ヘッダ名。全MCPリクエストに付与し、cc-memory server 再起動を
# またいで安定な呼び出し元識別子として relay 側（identity.py）が読む。
BRIDGE_SESSION_HEADER = "X-CC-Memory-Bridge-Session-Id"

HEARTBEAT_INTERVAL_ENV = "CC_MEMORY_LAUNCHER_HEARTBEAT_SEC"
DEFAULT_HEARTBEAT_INTERVAL_SEC = 60.0


def _read_heartbeat_interval_sec() -> float:
    """env `CC_MEMORY_LAUNCHER_HEARTBEAT_SEC` から heartbeat 間隔を読む。

    未設定・無効値・0以下の場合は既定値にフォールバックする。
    """
    raw = os.environ.get(HEARTBEAT_INTERVAL_ENV)
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


class ServerDisconnected(Exception):
    """サーバー側の切断を示す例外。stdin EOFとの区別に使用する。"""
    pass


# サーバー接続設定
# CC_MEMORY_URL が設定されていればそのURLを使い、未設定ならローカルHTTPサーバーに接続する。
# リモートURL指定時はサーバー自動起動・セッション管理をスキップする。
_REMOTE_URL = os.environ.get("CC_MEMORY_URL")

if _REMOTE_URL:
    if not _REMOTE_URL.startswith(("http://", "https://")):
        raise ValueError(
            f"CC_MEMORY_URL must start with http:// or https://, got: {_REMOTE_URL!r}"
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
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        # 4xx系HTTPエラーは「サーバー起動済み」を意味する
        return e.code in (405, 406, 400)
    except Exception:
        return False


def _start_http_server() -> bool:
    """HTTPサーバーをデーモンとして起動する。

    sys.executableは.mcp.jsonの「uv run python -m src.launcher」経由で
    起動されることを前提とし、uv仮想環境のPython（.venv/bin/python）を使用する。
    """
    try:
        subprocess.Popen(
            [sys.executable, "-m", "src.main", "--transport", "http"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
    # ただしプロセスが死んでいる場合はstale lockとして削除し、新規起動する。
    from src.infra.lock_file import read as read_lock, is_process_alive
    from src.infra.lock_file import LOCK_FILE

    lock_info = read_lock()
    if lock_info is not None and not is_process_alive(lock_info["pid"]):
        logger.info(f"Removing stale lock file: pid={lock_info['pid']}")
        LOCK_FILE.unlink(missing_ok=True)
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


def _register_session() -> bool:
    """セッション登録（POST /session/register）"""
    try:
        data = json.dumps({"session_id": _session_id}).encode("utf-8")
        req = urllib.request.Request(
            SESSION_REGISTER_URL,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
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
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            logger.info(f"Session unregistered: {result}")
            return True
    except Exception as e:
        logger.warning(f"Session unregister failed: {e}")
        return False


def _cleanup():
    """セッション解除 + ログ出力"""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    _unregister_session()


# =============================================
# stdio <-> HTTP ブリッジ
# =============================================


async def _bridge() -> None:
    """stdinからJSON-RPCメッセージを読み、HTTP POST /mcpに転送し、レスポンスをstdoutに書く。

    MCP SDK の streamable_http_client を利用し、ストリーム間のブリッジを行う。
    正常終了（stdin EOF）時はreturn、サーバー側切断時はServerDisconnectedをraiseする。
    """
    # 遅延import: デーモン起動ロジックはMCP SDKに依存しないため、
    # ブリッジ実行時まで重いimportを遅延させて起動速度を確保する
    import anyio
    from mcp import types
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp.shared.message import SessionMessage

    # stdin EOFとサーバー切断を区別するためのフラグ
    # stdin EOF: stdin_to_serverが先に終了 → 正常終了
    # サーバー切断: server_to_stdoutが先に終了 → stdin_eofがFalse → ServerDisconnected
    stdin_eof = False

    # 全MCPリクエストに bridge identity ヘッダを同梱する。cc-memory server が
    # 再起動しても launcher プロセス（＝ _session_id）が生きている限り不変な値で、
    # relay の declaration/inbox/subscription キー解決（identity.py）が読む。
    http_client = create_mcp_http_client(headers={BRIDGE_SESSION_HEADER: _session_id})
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

            async def stdin_to_server() -> None:
                """stdinから1行ずつ読み、write_streamに送る。"""
                nonlocal stdin_eof
                loop = asyncio.get_running_loop()
                reader = asyncio.StreamReader()
                transport, _ = await loop.connect_read_pipe(
                    lambda: asyncio.StreamReaderProtocol(reader),
                    sys.stdin.buffer,
                )
                try:
                    buffer = b""
                    while True:
                        chunk = await reader.read(65536)
                        if not chunk:
                            stdin_eof = True
                            break
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                message = types.JSONRPCMessage.model_validate_json(line)
                                session_msg = SessionMessage(message)
                                await write_stream.send(session_msg)
                            except Exception:
                                logger.exception("Failed to parse stdin message")
                except Exception:
                    stdin_eof = True  # stdin エラーも「stdin 側起因」として扱う
                    logger.debug("stdin reader ended")
                finally:
                    if buffer.strip():
                        logger.warning(
                            f"Discarding {len(buffer)} bytes of incomplete data in stdin buffer"
                        )
                    transport.close()
                    await write_stream.aclose()

            async def server_to_stdout() -> None:
                """read_streamからメッセージを受信し、stdoutに書く。

                read_streamが終了したとき、stdin_eofがFalseならサーバー側切断と判断し
                ServerDisconnectedをraiseしてtask group全体をキャンセルする。
                """
                try:
                    async for session_msg_or_exc in read_stream:
                        if isinstance(session_msg_or_exc, Exception):
                            logger.warning(f"Received exception from server: {session_msg_or_exc}")
                            continue
                        message = session_msg_or_exc.message
                        json_bytes = message.model_dump_json(
                            by_alias=True, exclude_none=True
                        ).encode("utf-8")

                        sys.stdout.buffer.write(json_bytes + b"\n")
                        sys.stdout.buffer.flush()
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
                """
                while True:
                    await anyio.sleep(HEARTBEAT_INTERVAL_SEC)
                    await anyio.to_thread.run_sync(_register_session)

            async with anyio.create_task_group() as tg:
                tg.start_soon(stdin_to_server)
                tg.start_soon(server_to_stdout)
                tg.start_soon(heartbeat_loop)


def main() -> None:
    """ランチャーのメインエントリーポイント

    サーバー側切断時は自動でリトライする。MAX_RETRIES が None なら無限、
    数値指定なら最大 MAX_RETRIES 回。stdin EOF（Claude Code終了）時は即座に終了する。
    """
    # ログ設定（stderrへ出力、stdoutはMCPプロトコル用）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [launcher] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    # セッション解除(atexit/SIGTERM)はローカル/リモード問わず常時登録する。
    # _unregister_session()は失敗を握りつぶすため、登録エンドポイントを持たない
    # 接続先（例: セッションAPIを持たないremote展開）でも安全に呼べる。
    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # atexitが発火する
    if not _IS_LOCAL:
        logger.info("Remote mode: connecting to %s", MCP_ENDPOINT)

    max_retries = MAX_RETRIES
    retries_label = "inf" if max_retries is None else str(max_retries)

    for attempt in itertools.count():
        # 1. HTTPサーバーの起動確認（ローカルのみ。リモートはOAuth等の制約があるためスキップ）
        if _IS_LOCAL and not _ensure_server_running():
            logger.error("Failed to ensure HTTP server is running")
            sys.exit(1)

        # 2. セッション登録（ローカル/リモート問わず試行する）。
        #    ローカルは登録失敗を致命エラーとして扱う（ローカルサーバーは常に
        #    このAPIを持つため、失敗は異常事態）。リモートは接続先がセッション
        #    APIを持たない場合があるため、警告ログのみで続行する（bridge identity
        #    ヘッダによる declaration/inbox 安定化自体はセッション登録の成否に
        #    依存しない。ただしこの場合 lease_loop の生存ゲート対象には含まれない）。
        registered = _register_session()
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
            asyncio.run(_bridge())
            break  # stdin EOF → 正常終了
        except KeyboardInterrupt:
            break
        except Exception as e:
            # anyioのExceptionGroupによりServerDisconnectedが直接キャッチできない
            # ケースがあるため、例外の種類を問わず統一的にリトライする
            if max_retries is not None and attempt >= max_retries:
                logger.error("Bridge failed, max retries (%d) exceeded: %s", max_retries, e)
                break
            backoff = min(2 ** (attempt + 1), BACKOFF_CAP_SEC)
            logger.warning(
                "Bridge failed (%s), retrying in %ds (%d/%s)",
                e, backoff, attempt + 1, retries_label,
            )
            time.sleep(backoff)

    _cleanup()


if __name__ == "__main__":
    main()
