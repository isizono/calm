"""ロックファイル管理モジュール

HTTPサーバーモードで使用するロックファイルの作成・読み取り・削除を行う。
ロックファイルにはPID・ポート情報を記録し、サーバーの多重起動を防止する。
"""
import json
import logging
import os
import socket
from pathlib import Path
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)

LOCK_DIR = Path.home() / ".cc-memory"
LOCK_FILE = LOCK_DIR / "server.lock"


class LockInfo(TypedDict):
    """ロックファイルに記録する情報"""
    pid: int
    port: int


def acquire(port: int) -> bool:
    """ロックファイルをアトミックに作成する。

    open('x')（O_CREAT | O_EXCL）でアトミックな排他作成を行う。
    既にファイルが存在する場合はstale判定を行い、staleなら削除して再試行する。
    プロセスが生存中であればFalseを返す。

    Args:
        port: サーバーのポート番号

    Returns:
        ロック取得に成功した場合True
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)

    info: LockInfo = {"pid": os.getpid(), "port": port}

    # まずアトミックな排他作成を試みる
    if _try_create_exclusive(info):
        return True

    # ファイルが既に存在する場合、stale判定
    existing = read()
    if existing is not None and is_process_alive(existing["pid"]):
        # PIDが生きていてもポートに応答がなければstale（PID再利用対策）
        if is_port_listening(existing["port"]):
            logger.warning(
                f"Server already running: pid={existing['pid']}, port={existing['port']}"
            )
            return False
        logger.info(
            f"Removing stale lock file: pid={existing['pid']} alive but port {existing['port']} not listening"
        )
    elif existing is not None:
        logger.info(f"Removing stale lock file: pid={existing['pid']} (process dead)")
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    return _try_create_exclusive(info)


def _try_create_exclusive(info: LockInfo) -> bool:
    """open('x')でアトミックにロックファイルを作成する。"""
    try:
        with open(LOCK_FILE, "x", encoding="utf-8") as f:
            f.write(json.dumps(info))
        logger.info(f"Lock file created: {LOCK_FILE}")
        return True
    except FileExistsError:
        return False
    except OSError as e:
        logger.error(f"Failed to create lock file: {e}")
        return False


def read() -> Optional[LockInfo]:
    """ロックファイルを読み取る。

    Returns:
        ロック情報。ファイルが存在しない or パースエラーの場合はNone
    """
    if not LOCK_FILE.exists():
        return None
    try:
        data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "pid" in data and "port" in data:
            return LockInfo(pid=data["pid"], port=data["port"])
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read lock file: {e}")
        return None


def release() -> None:
    """ロックファイルを削除する。

    自プロセスのPIDと一致する場合のみ削除する。
    ファイルが存在しない場合は何もしない。
    """
    existing = read()
    if existing is None:
        return
    if existing["pid"] != os.getpid():
        logger.warning(
            f"Lock file owned by another process: pid={existing['pid']}, skipping release"
        )
        return
    try:
        LOCK_FILE.unlink()
        logger.info("Lock file released")
    except OSError as e:
        logger.warning(f"Failed to release lock file: {e}")


def is_process_alive(pid: int) -> bool:
    """指定PIDのプロセスが生存しているか確認する。"""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # プロセスは存在するが権限がない → 生存している
        return True


def is_port_listening(port: int, host: str = "localhost", timeout: float = 1.0) -> bool:
    """指定ポートにTCP接続できるか確認する。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False
