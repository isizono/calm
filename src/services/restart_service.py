"""cc-memory MCPサーバー・embeddingサーバーの強制再起動ロジック

launcher.py の _ensure_server_running() は「生きていれば何もしない」ensure動作であり、
プラグインアップデート後にコード変更が反映されない。本モジュールは既存プロセスを
明示的に終了させてから新規プロセスを起動する「強制入れ替え」を提供する。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

from src.http_config import HTTP_PORT
from src.services.embedding_service import PORT as EMBEDDING_SERVER_PORT

MCP_PORT = HTTP_PORT
EMBEDDING_PORT = EMBEDDING_SERVER_PORT
LAUNCHER_LOG_PATH = Path.home() / ".cc-memory" / "logs" / "restart_launcher.log"

DEFAULT_START_TIMEOUT_SEC = 30.0
DEFAULT_POLL_INTERVAL_SEC = 0.5
DEFAULT_KILL_WAIT_SEC = 10.0
DEFAULT_KILL_ESCALATE_SEC = 5.0
DEFAULT_SYNC_TIMEOUT_SEC = 600.0
SUBPROCESS_TIMEOUT_SEC = 5.0


def find_listen_pids(port: int) -> list[int]:
    """指定ポートでLISTEN中のPIDを返す。

    -sTCP:LISTEN条件で絞り込むことで、接続中のクライアント(ブリッジ等)を
    巻き添えにしない。lsofがハングした場合はタイムアウトし、空リストとして扱う
    (「わからない」を「いない」として安全側に倒す。再起動フロー全体を
    無期限にブロックしないことを優先する)。
    """
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, check=False, timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return []
    return sorted({int(p) for p in result.stdout.split() if p.strip()})


def process_start_signature(pid: int) -> str | None:
    """プロセスの起動時刻を返す。プロセスが存在しなければNone。

    LISTEN確認だけでは、PID再利用や検出タイミングのズレで
    古いプロセスを新規と誤認しうる。起動時刻の比較でこれを防ぐ。
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, check=False, timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return None
    output = result.stdout.strip()
    return output or None


def _process_alive(pid: int) -> bool:
    """シグナル0の送信でプロセスの生死を確認する(実際にはシグナルを送らない)。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_pids(
    pids: list[int],
    *,
    escalate_after_sec: float = DEFAULT_KILL_ESCALATE_SEC,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
) -> None:
    """各PIDにSIGTERMを送り、escalate_after_sec待っても生存していればSIGKILLで強制終了する。

    SIGTERMのみで終了しないプロセスを生かしたまま次の処理に進むと、
    新規サーバーがポートのbindに失敗して見えない失敗を招く。
    """
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + escalate_after_sec
    remaining = {pid for pid in pids if _process_alive(pid)}
    while remaining and time.monotonic() < deadline:
        time.sleep(poll_interval_sec)
        remaining = {pid for pid in remaining if _process_alive(pid)}

    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class RestartResult(NamedTuple):
    ok: bool
    old_pids: list[int]
    new_pids: list[int]
    detail: str


class SyncResult(NamedTuple):
    ok: bool
    duration_sec: float
    detail: str


def sync_dependencies(
    project_root: Path,
    *,
    timeout_sec: float = DEFAULT_SYNC_TIMEOUT_SEC,
) -> SyncResult:
    """`uv sync` でvenvを再構築する。

    旧サーバーがまだポートを握っている間に実行することで、後続の
    kill→起動→30秒監視のダウンタイムからvenv構築時間を切り離す。
    失敗しても呼び出し側は後続の再起動処理を続行してよい。
    """
    # 実行中のインタープリタ自体がproject_root配下の.venvから起動している
    # ケース(uv run --directory経由の起動)があるため、この呼び出しの後に
    # 新規の外部パッケージimportを追加しない。sync前に読み込み済みのモジュールは
    # sys.modulesにキャッシュされ影響を受けないが、未import分をここより後で
    # 遅延importすると、venv差し替え中の欠損ファイルを踏む可能性がある。
    start = time.monotonic()
    try:
        result = subprocess.run(
            ["uv", "sync", "--directory", str(project_root)],
            capture_output=True, text=True, check=False, timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return SyncResult(False, time.monotonic() - start, f"uv sync timed out after {timeout_sec}s")

    duration = time.monotonic() - start
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "uv sync failed"
        return SyncResult(False, duration, detail)
    return SyncResult(True, duration, "synced")


def _is_replaced(old_signatures: dict[int, str | None], new_pids: list[int]) -> bool:
    """new_pidsの中に、旧プロセスの記録と一致しないもの(=新規)が1つでもあればTrue"""
    for pid in new_pids:
        if pid not in old_signatures:
            return True
        if old_signatures[pid] != process_start_signature(pid):
            return True
    return False


def restart_mcp_server(
    project_root: Path,
    *,
    start_timeout_sec: float = DEFAULT_START_TIMEOUT_SEC,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
    kill_wait_sec: float = DEFAULT_KILL_WAIT_SEC,
) -> RestartResult:
    """MCPサーバー本体を強制的に再起動する。

    既存プロセスをkillしてから新規launcherプロセスを起動し、
    新PIDの起動時刻が旧PIDの記録と一致しないことをもって
    「新規プロセスへの入れ替わり」を確認してから成功とみなす。
    """
    old_pids = find_listen_pids(MCP_PORT)
    old_signatures = {pid: process_start_signature(pid) for pid in old_pids}

    if old_pids:
        kill_pids(old_pids)
        deadline = time.monotonic() + kill_wait_sec
        while time.monotonic() < deadline and find_listen_pids(MCP_PORT):
            time.sleep(poll_interval_sec)

    LAUNCHER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LAUNCHER_LOG_PATH, "w") as log_file:
        proc = subprocess.Popen(
            ["uv", "run", "--directory", str(project_root), "python", "-m", "src.launcher"],
            start_new_session=True,
            stdout=log_file,
            stderr=log_file,
            cwd=str(project_root),
        )

    deadline = time.monotonic() + start_timeout_sec
    while time.monotonic() < deadline:
        new_pids = find_listen_pids(MCP_PORT)
        if new_pids and _is_replaced(old_signatures, new_pids):
            return RestartResult(True, old_pids, new_pids, "restarted")
        time.sleep(poll_interval_sec)

    _kill_process_group(proc)
    return RestartResult(
        False, old_pids, find_listen_pids(MCP_PORT),
        f"server did not come up on port {MCP_PORT} within {start_timeout_sec}s",
    )


def _kill_process_group(proc: subprocess.Popen) -> None:
    """start_new_sessionで分離したプロセスグループごと終了させる。

    proc.kill()単体では`uv run ... python -m src.launcher`という
    ラッパー経由で起動した孫プロセス(実体のlauncher)が生き残ることがある。
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_embedding_server() -> list[int]:
    """embeddingサーバーを停止する。再起動はしない(次回encode呼び出し時にlazy spawnされる)。"""
    pids = find_listen_pids(EMBEDDING_PORT)
    kill_pids(pids)
    return pids


def clean_caches(project_root: Path) -> dict:
    """__pycache__を削除する。

    .venv配下は対象外にする。依存パッケージのバイトコードキャッシュまで
    削除すると、直後に起動する新規サーバーが全依存を再コンパイルする
    羽目になり、起動監視のタイムアウトを縮めるどころか悪化させる。
    """
    removed_pycache_dirs = []
    for pycache in project_root.rglob("__pycache__"):
        if ".venv" in pycache.parts:
            continue
        if pycache.is_dir():
            shutil.rmtree(pycache)
            removed_pycache_dirs.append(str(pycache))

    return {"removed_pycache_dirs": removed_pycache_dirs}


def restart_all(project_root: Path) -> dict:
    """依存関係の同期・キャッシュ掃除・MCP再起動・embedding停止を順に行う。

    uv syncとキャッシュ掃除は、旧MCPサーバーがまだ稼働している間に
    済ませておく。これによりkill〜新規プロセス起動〜起動監視という
    ダウンタイムの区間からvenv構築時間を切り離す。uv syncが失敗しても
    後続のMCP再起動は試行する(結果には成否を含めて返す)。
    """
    sync_result = sync_dependencies(project_root)
    cache_result = clean_caches(project_root)
    mcp_result = restart_mcp_server(project_root)
    embedding_stopped = stop_embedding_server()
    return {
        "uv_sync": {
            "ok": sync_result.ok,
            "duration_sec": round(sync_result.duration_sec, 3),
            "detail": sync_result.detail,
        },
        "mcp_server": {
            "ok": mcp_result.ok,
            "old_pids": mcp_result.old_pids,
            "new_pids": mcp_result.new_pids,
            "detail": mcp_result.detail,
        },
        "embedding_server": {"stopped_pids": embedding_stopped},
        "caches": cache_result,
    }


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent.parent
    result = restart_all(project_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["mcp_server"]["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
