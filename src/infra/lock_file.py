"""ロックファイル管理モジュール

HTTPサーバーモードで使用するロックファイルの作成・読み取り・削除を行う。
ロックファイルにはPID・ポート情報を記録し、サーバーの多重起動を防止する。
"""
import json
import logging
import os
import socket
import sys
from pathlib import Path
from typing import Optional, TypedDict

import psutil

from src.infra.process_signature import process_start_signature

logger = logging.getLogger(__name__)

LOCK_DIR = Path.home() / ".cc-memory"
LOCK_FILE = LOCK_DIR / "server.lock"


class LockInfo(TypedDict):
    """ロックファイルに記録する情報"""
    pid: int
    port: int
    start_time: Optional[str]


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

    info: LockInfo = {
        "pid": os.getpid(),
        "port": port,
        "start_time": process_start_signature(os.getpid()),
    }

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
            return LockInfo(pid=data["pid"], port=data["port"], start_time=data.get("start_time"))
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read lock file: {e}")
        return None


def release(pid: Optional[int] = None) -> None:
    """ロックファイルを削除する。

    `pid`が記録されたpidと一致する場合のみ削除する。省略時は呼び出し元
    プロセス自身のpidで判定する（サーバー自身がシャットダウン時に呼ぶ既定経路）。
    外部から強制終了させたプロセスの後始末（Windowsの`TerminateProcess`は
    対象プロセスのfinally節を経由しないため`release()`が走らない）にも
    同じ関数を使えるよう、判定対象のpidを明示できるようにしている。
    ファイルが存在しない場合は何もしない。
    """
    target_pid = os.getpid() if pid is None else pid
    existing = read()
    if existing is None:
        return
    if existing["pid"] != target_pid:
        logger.warning(
            f"Lock file owned by another process: pid={existing['pid']}, skipping release"
        )
        return
    try:
        LOCK_FILE.unlink()
        logger.info("Lock file released")
    except OSError as e:
        logger.warning(f"Failed to release lock file: {e}")


def is_lock_stale(info: LockInfo) -> bool:
    """ロックファイルの指すサーバーがもう存在しないとみなせるか判定する。

    PIDが死んでいれば無条件でstale。生きていても、ロック作成時に記録した
    起動時刻と現在そのPIDが示すプロセスの起動時刻が食い違えば、PID再利用
    （別プロセスが同じPIDを引き継いだ）とみなしstale扱いにする。
    記録が無い（旧形式のロックファイル）場合や起動時刻が取得できない場合は
    比較しようがないため、PID生存の結果をそのまま使う。
    """
    if not is_process_alive(info["pid"]):
        return True
    recorded = info.get("start_time")
    if recorded is None:
        return False
    current = process_start_signature(info["pid"])
    return current is not None and current != recorded


def is_process_alive(pid: int) -> bool:
    """指定PIDのプロセスが生存しているか確認する。

    ゾンビ（defunct）プロセスはpsutilの存在確認だけでは「生存中」と判定
    されてしまうため、別途 `_is_zombie()` でstatusを確認して死亡扱いにする。
    """
    return psutil.pid_exists(pid) and not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """psutilのstatusがゾンビ（zombie）かを確認する。

    プロセス消滅・権限不足で判定できない場合は「ゾンビではない」扱いにする
    （「わからない」を安全側＝生存扱いに倒し、正常プロセスの誤stale化を避ける）。
    Windowsのpsutil.status()はSTOPPED/RUNNINGしか返さずゾンビ概念自体が
    無い（常にFalse）ため、判定のためだけに全プロセスを列挙するコストを
    避けて先に返す。
    """
    if sys.platform == "win32":
        return False
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def is_port_listening(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """指定ポートにTCP接続できるか確認する。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False
