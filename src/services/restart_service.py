"""calm MCPサーバー・embeddingサーバーの強制再起動ロジック

launcher.py の _ensure_server_running() は「生きていれば何もしない」ensure動作であり、
プラグインアップデート後にコード変更が反映されない。本モジュールは既存プロセスを
明示的に終了させてから新規プロセスを起動する「強制入れ替え」を提供する。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import psutil

from src.env_compat import env_get, env_set
from src.http_config import EMBEDDING_PORT, HTTP_PORT
from src.infra import lock_file
from src.infra.detached_process import DetachedProcess, popen_detached
from src.infra.git_repo import resolve_main_repo_root
from src.infra.lock_file import is_process_alive
from src.infra.process_signature import process_start_signature

# EMBEDDING_PORTはsrc.http_configから直接取る(src.services.embedding_service
# 経由だと、Windowsで本CLI自身の--no-sync実行中にsqlite_vec/numpy等の重い
# 依存一式がimport時にロードされ、後続のuv sync(subprocess)がそれらの
# ファイルを更新しようとした際に競合しうるため)。
MCP_PORT = HTTP_PORT
LAUNCHER_LOG_PATH = Path.home() / ".cc-memory" / "logs" / "restart_launcher.log"

DEFAULT_START_TIMEOUT_SEC = 30.0
DEFAULT_POLL_INTERVAL_SEC = 0.5
DEFAULT_KILL_WAIT_SEC = 10.0
DEFAULT_KILL_ESCALATE_SEC = 5.0
DEFAULT_SYNC_TIMEOUT_SEC = 600.0
SUBPROCESS_TIMEOUT_SEC = 5.0
ORPHAN_MARKER_NAME = ".orphaned_at"
PRUNE_LSOF_TIMEOUT_SEC = 15.0


def find_listen_pids(port: int) -> list[int]:
    """指定ポートでLISTEN中のPIDを返す。

    Windowsにはlsofが無いためpsutilで代替する(POSIXは引き続きlsofを使う。
    -sTCP:LISTEN条件で絞り込むことで、接続中のクライアント(ブリッジ等)を
    巻き添えにしない)。lsofがハングした場合はタイムアウトし、空リストとして扱う
    (「わからない」を「いない」として安全側に倒す。再起動フロー全体を
    無期限にブロックしないことを優先する)。
    """
    if sys.platform == "win32":
        return _find_listen_pids_windows(port)
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, encoding="utf-8", check=False, timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return []
    return sorted({int(p) for p in result.stdout.split() if p.strip()})


def _find_listen_pids_windows(port: int) -> list[int]:
    """psutilでLISTEN中のTCP接続を列挙し、該当ポートのPIDを返す。

    一部の接続は権限不足でpidがNoneになりうる(他ユーザーのプロセス等)ため除外する。
    """
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError):
        return []
    return sorted({
        c.pid for c in conns
        if c.pid is not None and c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port
    })


def kill_pids(
    pids: list[int],
    *,
    escalate_after_sec: float = DEFAULT_KILL_ESCALATE_SEC,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
) -> None:
    """各PIDを終了させる。

    POSIXではSIGTERMを送り、escalate_after_sec待っても生存していればSIGKILLで
    強制終了する(SIGTERMのみで終了しないプロセスを生かしたまま次の処理に進むと、
    新規サーバーがポートのbindに失敗して見えない失敗を招く)。

    Windowsではsignal.SIGKILLが存在せず、os.kill(pid, signal.SIGTERM)も
    TerminateProcessとして即座に終了するだけでSIGTERMの猶予的な意味を持たない
    ため、エスカレーションの概念自体が無い。psutilのterminate()を1回送って
    生存確認で待つだけにする。
    """
    if sys.platform == "win32":
        _kill_pids_windows(pids, wait_sec=escalate_after_sec, poll_interval_sec=poll_interval_sec)
        return

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + escalate_after_sec
    remaining = {pid for pid in pids if is_process_alive(pid)}
    while remaining and time.monotonic() < deadline:
        time.sleep(poll_interval_sec)
        remaining = {pid for pid in remaining if is_process_alive(pid)}

    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _kill_pids_windows(pids: list[int], *, wait_sec: float, poll_interval_sec: float) -> None:
    """psutil.Process.terminate()(TerminateProcess相当)を1回送り、生存確認で待つ。"""
    for pid in pids:
        try:
            psutil.Process(pid).terminate()
        except psutil.NoSuchProcess:
            pass

    deadline = time.monotonic() + wait_sec
    remaining = {pid for pid in pids if is_process_alive(pid)}
    while remaining and time.monotonic() < deadline:
        time.sleep(poll_interval_sec)
        remaining = {pid for pid in remaining if is_process_alive(pid)}


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

    POSIXでは旧サーバーがまだポートを握っている間に実行することで、後続の
    kill→起動→30秒監視のダウンタイムからvenv構築時間を切り離す
    (Windowsではサーバーを止めた後に呼ぶため、この効果は無い)。
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
            capture_output=True, text=True, encoding="utf-8", check=False, timeout=timeout_sec,
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


# project_root(`Path(__file__).resolve().parent.parent.parent`)は、このモジュール
# 自身がworktree配下のチェックアウトから実行された場合、main repoルートではなく
# そのworktreeのルートを指す。embedding_service._resolve_project_root()が
# worktree誤解決によるメモリ膨張事故の再発防止のため`__file__`ベースの解決を
# 意図的に避けている（launcher.pyの_propagate_plugin_root_env()のdocstring参照）
# のと同じ理由で、CALM_PROJECT_ROOTにはproject_rootをそのまま書き込まず、
# git_repo.resolve_main_repo_root()を経由してmain repoルートに正規化する
# (launcher.py側も同じ解決ロジックを必要とするため実装を共有モジュールに置く)。
_resolve_main_repo_root = resolve_main_repo_root


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
    old_pids, old_signatures = _stop_mcp_server(kill_wait_sec, poll_interval_sec)
    return _start_mcp_server(
        project_root, old_pids, old_signatures,
        start_timeout_sec=start_timeout_sec, poll_interval_sec=poll_interval_sec,
    )


def _stop_mcp_server(kill_wait_sec: float, poll_interval_sec: float) -> tuple[list[int], dict[int, str | None]]:
    """既存のMCPサーバープロセスを止め、入れ替え判定用の旧PID・起動時刻記録を返す。

    Windowsのterminate(TerminateProcess相当)はrelease()のfinally節を経由しない
    ため、停止確認後もserver.lockが残り続ける(POSIXでも、SIGTERMを無視し
    SIGKILLで強制終了したケースは同様にfinally節を経由しない)。殺したpidと
    記録pidが一致し、かつ既に死んでいる場合だけ明示的に消す。生存中に誤って
    消さないための確認であり、停止後に別プロセスが新たにロックを取り直して
    いた場合に誤って消さないための確認でもある。
    """
    old_pids = find_listen_pids(MCP_PORT)
    old_signatures = {pid: process_start_signature(pid) for pid in old_pids}

    if old_pids:
        kill_pids(old_pids)
        deadline = time.monotonic() + kill_wait_sec
        while time.monotonic() < deadline and find_listen_pids(MCP_PORT):
            time.sleep(poll_interval_sec)
        for pid in old_pids:
            if not is_process_alive(pid):
                lock_file.release(pid)

    return old_pids, old_signatures


def _start_mcp_server(
    project_root: Path,
    old_pids: list[int],
    old_signatures: dict[int, str | None],
    *,
    start_timeout_sec: float,
    poll_interval_sec: float,
) -> RestartResult:
    """新規launcherプロセスを起動し、ポートの入れ替わりを確認する。"""
    # 新規launcherプロセスはos.environを継承する(Popenにenv未指定)。プラグイン
    # キャッシュ配置(gitリポジトリ外)ではlauncher起動時の_propagate_plugin_root_env()
    # がCLAUDE_PLUGIN_ROOT頼みで、この再起動フロー経由の子プロセスにその値が
    # 伝播している保証がないため、embedding_service._resolve_project_root()の
    # git rev-parseフォールバックが失敗してembeddingサーバーが起動できなくなる。
    # ここで明示的に設定し、子プロセスチェーン全体に伝播させる
    # (_resolve_main_repo_root()でworktree誤解決を避ける)。
    if not env_get("CALM_PROJECT_ROOT"):
        env_set("CALM_PROJECT_ROOT", str(_resolve_main_repo_root(project_root)))

    # .mcp.jsonのcalm.env経由ではなくこの再起動フローから直接launcherを起動する
    # ため、.mcp.jsonのPYTHONUTF8=1がここでは伝播しない。Windows既定のANSIコード
    # ページ下で新規DBのmigrationを読む際に文字化け・UnicodeDecodeErrorを防ぐため
    # 明示的に設定する(CALM_プレフィックス専用のenv_set/env_getはPYTHONUTF8には
    # 使えないため、os.environを直接操作する)。
    os.environ.setdefault("PYTHONUTF8", "1")

    LAUNCHER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LAUNCHER_LOG_PATH, "w") as log_file:
        proc = popen_detached(
            ["uv", "run", "--directory", str(project_root), "python", "-m", "src.launcher"],
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


def _kill_process_group(proc: DetachedProcess) -> None:
    """起動に失敗した新規launcherプロセスを、子孫ごと終了させる。

    proc.kill()単体では`uv run ... python -m src.launcher`という
    ラッパー経由で起動した孫プロセス(実体のlauncher)が生き残ることがある。

    POSIXはプロセスグループ(os.killpg/os.getpgid)で子孫をまとめて終了できる。
    Windowsには相当する機構が無いため、psutilで直下の子プロセスまでを
    終了させる(詳細は_kill_process_group_windows参照)。
    """
    if sys.platform == "win32":
        _kill_process_group_windows(proc.pid)
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


def _kill_process_group_windows(pid: int) -> None:
    """psutilで直下の子プロセス(uv run経由のvenvリダイレクタ等)まで含めてterminateする。

    孫以降は意図的に対象外にする。launcher.py自身が起動する実サーバー(HTTPサーバー)は
    popen_detachedでCREATE_NEW_PROCESS_GROUPを付けて切り離されるが、Windowsの
    ppidはこのフラグの影響を受けず起動元を指したままになるため、
    children(recursive=True)で辿ると切り離したはずのサーバーまで終了させてしまう。
    """
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=False)
    for proc in [parent, *children]:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
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


def _has_open_file_handles(path: Path) -> bool:
    """path配下のファイルを現在開いているプロセスが1つでもあるか。

    `find_listen_pids()`とは判定不能時の安全側の向きが逆であることに注意。
    あちらは「わからない=いない」(誤ってkillしない方が安全)だが、
    ここでは「わからない=いる」(誤って削除しない方が安全)にする。
    """
    try:
        result = subprocess.run(
            ["lsof", "+D", str(path)],
            capture_output=True, text=True, encoding="utf-8", check=False, timeout=PRUNE_LSOF_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return True
    return bool(result.stdout.strip())


def prune_orphaned_plugin_versions(project_root: Path) -> dict:
    """`project_root`の兄弟ディレクトリのうち、使われなくなった旧バージョンを削除する。

    プラグインキャッシュ配置では`project_root`自体が
    `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`であり、
    兄弟ディレクトリは同じプラグインの別バージョンにあたる。Claude Code本体が
    現在の使用対象でなくなったバージョンに`.orphaned_at`マーカーを書くため、
    それを削除条件の必須シグナルとして使う(自前で「どれが最新か」を
    再判定しない。マーカーが将来書かれなくなった場合は削除が単に止まるだけで
    誤削除方向には振れない、という非対称性がこの設計の安全性の根拠)。

    `project_root`自身がgitリポジトリ(worktree含む)の場合は何もしない。
    その場合の兄弟ディレクトリは開発用チェックアウトの並びであり、
    プラグインバージョンの並びではないため。

    マーカーがあっても、同じディレクトリを指す`uv run --directory`経由の
    launcherプロセス(他セッションがまだ旧バージョンから接続中)が生きている
    可能性があるため、削除直前に`lsof`でオープン中のファイルハンドルが
    無いことも確認する。
    """
    removed = []
    skipped = []

    if (project_root / ".git").exists():
        return {"removed": removed, "skipped": skipped}

    versions_root = project_root.parent
    if not versions_root.is_dir():
        return {"removed": removed, "skipped": skipped}

    current = project_root.resolve()
    for entry in sorted(versions_root.iterdir()):
        if not entry.is_dir() or entry.resolve() == current:
            continue
        if not (entry / ORPHAN_MARKER_NAME).exists():
            skipped.append({"path": str(entry), "reason": "not marked orphaned"})
            continue
        if _has_open_file_handles(entry):
            skipped.append({"path": str(entry), "reason": "open file handles"})
            continue
        shutil.rmtree(entry)
        removed.append(str(entry))

    return {"removed": removed, "skipped": skipped}


def restart_all(project_root: Path, *, restart_embedding: bool = False) -> dict:
    """依存関係の同期・キャッシュ掃除・MCP再起動を順に行う。

    POSIXではuv syncとキャッシュ掃除を、旧MCPサーバーがまだ稼働している間に
    済ませておく。これによりkill〜新規プロセス起動〜起動監視という
    ダウンタイムの区間からvenv構築時間を切り離す。uv syncが失敗しても
    後続のMCP再起動は試行する(結果には成否を含めて返す)。

    Windowsでは稼働中のサーバーが`.venv`配下のファイルを開いたままにするため、
    POSIXと同じ順序でsyncすると差し替え対象のファイルが使用中で失敗しうる。
    サーバーを先に止めてからsyncする。

    embeddingサーバーはコードの変更頻度が低いため、既定では停止しない
    (次にencodeが必要になったとき自動でlazy spawnされるだけで、都度停止すると
    モデル再ロード分の起動遅延を毎回背負うだけでメリットが薄い)。
    明示的にコード変更を反映させたい場合のみ `restart_embedding=True` を指定する。

    プラグインキャッシュの旧バージョン掃除(prune_orphaned_plugin_versions)は
    MCP再起動が成功した場合のみ行う。再起動自体が失敗している状況で
    キャッシュディレクトリまで変化させると、原因調査中の変数を増やすだけになる。
    """
    if sys.platform == "win32":
        old_pids, old_signatures = _stop_mcp_server(DEFAULT_KILL_WAIT_SEC, DEFAULT_POLL_INTERVAL_SEC)
        sync_result = sync_dependencies(project_root)
        cache_result = clean_caches(project_root)
        mcp_result = _start_mcp_server(
            project_root, old_pids, old_signatures,
            start_timeout_sec=DEFAULT_START_TIMEOUT_SEC, poll_interval_sec=DEFAULT_POLL_INTERVAL_SEC,
        )
    else:
        sync_result = sync_dependencies(project_root)
        cache_result = clean_caches(project_root)
        mcp_result = restart_mcp_server(project_root)
    embedding_stopped = stop_embedding_server() if restart_embedding else []
    prune_result = prune_orphaned_plugin_versions(project_root) if mcp_result.ok else {
        "removed": [], "skipped": [],
    }
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
        "plugin_cache_prune": prune_result,
    }


def _process_started_at_iso(pid: int) -> str | None:
    """プロセスの起動時刻をISO8601（UTC）文字列で返す。取得できなければNone。

    `/health`エンドポイントの`started_at`と同じ形式にすることで、人間・LLMが
    「どちらが新しいか」を文字列のまま比較できるようにする
    (`process_start_signature()`が返す不透明な値は等価比較専用で、
    この用途には使わない)。
    """
    try:
        return datetime.fromtimestamp(psutil.Process(pid).create_time(), tz=timezone.utc).isoformat()
    except psutil.Error:
        return None


def get_status() -> dict:
    """MCP/embeddingサーバーの現在の稼働状況を返す(副作用なし)。"""
    def _server_info(port: int) -> dict:
        pids = find_listen_pids(port)
        return {
            "port": port,
            "pids": pids,
            "running": bool(pids),
            "started_at": _process_started_at_iso(pids[0]) if pids else None,
        }

    return {
        "mcp_server": _server_info(MCP_PORT),
        "embedding_server": _server_info(EMBEDDING_PORT),
    }


def stop_all(*, stop_embedding: bool = False) -> dict:
    """再起動せず、MCPサーバー(と指定時はembeddingサーバー)を停止するだけで終了する。"""
    old_pids, _ = _stop_mcp_server(DEFAULT_KILL_WAIT_SEC, DEFAULT_POLL_INTERVAL_SEC)
    embedding_stopped = stop_embedding_server() if stop_embedding else []
    return {
        "mcp_server": {"stopped_pids": old_pids},
        "embedding_server": {"stopped_pids": embedding_stopped},
    }


def main() -> None:
    # Windows既定のANSIコードページ（cp932等）ではstdoutが非UTF-8になり、
    # json.dumps(..., ensure_ascii=False)が出す日本語（detailメッセージ等）が
    # UnicodeEncodeErrorで落ちうる。再起動自体は成功していてもこの出力で
    # 落ちると「成功したのにexit 1」になる。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="calm server restart")
    parser.add_argument(
        "--restart-embedding",
        action="store_true",
        help=(
            "embeddingサーバーも停止する(既定では停止しない。次回のencode呼び出し時に"
            "自動でlazy spawnされる)"
        ),
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--status", action="store_true",
        help="状態を確認するだけで何も変更しない",
    )
    mode_group.add_argument(
        "--stop", action="store_true",
        help="再起動せず、MCPサーバー(と--restart-embedding指定時はembeddingサーバー)を停止するだけで終了する",
    )
    args = parser.parse_args()

    if args.status:
        print(json.dumps(get_status(), ensure_ascii=False, indent=2))
        return

    if args.stop:
        print(json.dumps(stop_all(stop_embedding=args.restart_embedding), ensure_ascii=False, indent=2))
        return

    project_root = Path(__file__).resolve().parent.parent.parent
    result = restart_all(project_root, restart_embedding=args.restart_embedding)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["mcp_server"]["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
