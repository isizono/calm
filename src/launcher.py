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
    from mcp.shared.message import SessionMessage

    # stdin EOFとサーバー切断を区別するためのフラグ
    # stdin EOF: stdin_to_serverが先に終了 → 正常終了
    # サーバー切断: server_to_stdoutが先に終了 → stdin_eofがFalse → ServerDisconnected
    stdin_eof = False

    async with streamable_http_client(
        url=MCP_ENDPOINT,
        terminate_on_close=False,
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

        async with anyio.create_task_group() as tg:
            tg.start_soon(stdin_to_server)
            tg.start_soon(server_to_stdout)


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

    if _IS_LOCAL:
        # ローカル接続: セッション管理あり
        atexit.register(_cleanup)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # atexitが発火する
    else:
        logger.info("Remote mode: connecting to %s", MCP_ENDPOINT)

    max_retries = MAX_RETRIES
    retries_label = "inf" if max_retries is None else str(max_retries)

    for attempt in itertools.count():
        # 1. HTTPサーバーの起動確認（ローカルのみ。リモートはOAuth等の制約があるためスキップ）
        if _IS_LOCAL and not _ensure_server_running():
            logger.error("Failed to ensure HTTP server is running")
            sys.exit(1)

        # 2. セッション登録（ローカルのみ。リモートサーバーにはセッションAPIがない）
        if _IS_LOCAL and not _register_session():
            logger.error("Failed to register session")
            sys.exit(1)

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

    if _IS_LOCAL:
        _cleanup()


if __name__ == "__main__":
    main()
