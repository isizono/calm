"""ファイルシステム陳腐化検知ウォッチドッグ

HTTPサーバーモードで使用する。プラグインアップデートでプロジェクトルート配下の
コードが更新されても、複数セッションが常時接続し続ける運用では
``session_manager.SessionManager`` のセッション数ベースのウォッチドッグ
（アクティブセッション0件 → 猶予期間 → shutdown）が実質発火しない。

このモジュールはセッション数と無関係に、起動時に読み込んだコードが
ディスク上の内容と乖離した（＝陳腐化した）ことを検知して自死する経路を
独立に提供する。session_manager.py とは責務が異なる
（セッション数管理 vs ファイルシステム陳腐化検知）ため、判定ロジックは
統合せず独立したスレッド・独立した状態機械として実装する。
"""
import hashlib
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from src.env_compat import env_get

logger = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL_SEC = 3600
CHECK_INTERVAL_ENV = "CALM_STALENESS_CHECK_INTERVAL_SEC"

# 短すぎると更新の書き込み途中（プラグイン展開の最中等、一時的に不整合な
# 状態）を拾って誤検知しやすく、長すぎるとshutdownまでの反映が遅れる。
# 秒〜数十秒のオーダーで、その間を取って20秒とした。
DEFAULT_DEBOUNCE_SEC = 20
DEBOUNCE_ENV = "CALM_STALENESS_DEBOUNCE_SEC"

# ディレクトリ名ベースの除外（どの深さに出現しても刈る）。
# `.in_use` はプラグインキャッシュ直下で接続中セッションのPIDごとに
# ファイルが作成・削除される運用中ディレクトリで、除外しないと他セッションの
# 接続/切断のたびに陳腐化と誤検知し「1時間おきに無条件で自死する」有害な
# 挙動になる（実機検証済み）。
EXCLUDED_DIR_NAMES = frozenset({
    "__pycache__",
    ".venv",
    ".git",
    ".pytest_cache",
    ".in_use",
    ".trees",
})

# 相対パスベースの除外（dirname単体では刈れないピンポイント指定）。
# `.claude` というdirname自体を刈ると、陳腐化検知の対象にすべき
# `.claude/agents/` `.claude/skills/`（プラグイン同梱ファイル）まで
# 巻き添えで除外されてしまうため、`.claude/worktrees` だけを名指しで除外する。
EXCLUDED_RELATIVE_DIRS = frozenset({
    os.path.join(".claude", "worktrees"),
})


def _read_check_interval_sec() -> float:
    """env `CALM_STALENESS_CHECK_INTERVAL_SEC` からチェック間隔を読む。

    未設定・空文字・無効値の場合は ``DEFAULT_CHECK_INTERVAL_SEC`` にフォールバックする。
    0 を指定すると watchdog を完全に無効化する（スレッドを起動しない）。
    """
    raw = env_get(CHECK_INTERVAL_ENV)
    if raw is None or raw == "":
        return DEFAULT_CHECK_INTERVAL_SEC
    try:
        value = float(raw)
    except ValueError:
        print(
            f"[staleness_watchdog] WARNING Invalid {CHECK_INTERVAL_ENV}={raw!r}, "
            f"falling back to default {DEFAULT_CHECK_INTERVAL_SEC}s",
            file=sys.stderr,
        )
        return DEFAULT_CHECK_INTERVAL_SEC
    if value < 0:
        print(
            f"[staleness_watchdog] WARNING {CHECK_INTERVAL_ENV} must be >= 0, "
            f"got {value}, falling back to default {DEFAULT_CHECK_INTERVAL_SEC}s",
            file=sys.stderr,
        )
        return DEFAULT_CHECK_INTERVAL_SEC
    return value


def _read_debounce_sec() -> float:
    """env `CALM_STALENESS_DEBOUNCE_SEC` からデバウンス秒数を読む。

    未設定・空文字・無効値の場合は ``DEFAULT_DEBOUNCE_SEC`` にフォールバックする。
    """
    raw = env_get(DEBOUNCE_ENV)
    if raw is None or raw == "":
        return DEFAULT_DEBOUNCE_SEC
    try:
        value = float(raw)
    except ValueError:
        print(
            f"[staleness_watchdog] WARNING Invalid {DEBOUNCE_ENV}={raw!r}, "
            f"falling back to default {DEFAULT_DEBOUNCE_SEC}s",
            file=sys.stderr,
        )
        return DEFAULT_DEBOUNCE_SEC
    if value < 0:
        print(
            f"[staleness_watchdog] WARNING {DEBOUNCE_ENV} must be >= 0, "
            f"got {value}, falling back to default {DEFAULT_DEBOUNCE_SEC}s",
            file=sys.stderr,
        )
        return DEFAULT_DEBOUNCE_SEC
    return value


class StalenessWatchdog:
    """起動時に読み込んだコードのファイルシステム上での陳腐化を検知するウォッチドッグ。

    スレッドセーフではあるが、``start()`` は1インスタンスにつき1回のみ呼ぶ想定
    （二重起動は考慮しない、session_manager.SessionManager と同じ前提）。

    比較は常に ``start()`` 呼び出し時点で計算した1回限りのベースラインハッシュに
    対して行う。チェックのたびにハッシュを更新して次回と比較する「ローリング」
    実装は行わない — それだと「サーバーが眠っている間にキャッシュが更新された」
    ケースで、チェックN回目とN+1回目が両方とも新ハッシュを見て「変化なし」と
    誤判定し、永久にshutdownしなくなる。

    ``check_interval_sec`` が ``None`` の場合は env
    ``CALM_STALENESS_CHECK_INTERVAL_SEC`` から値を読む。0 を指定すると
    watchdogスレッド自体を起動しない。

    ``debounce_sec`` が ``None`` の場合は env ``CALM_STALENESS_DEBOUNCE_SEC``
    から値を読む。
    """

    def __init__(
        self,
        project_root: Path,
        check_interval_sec: Optional[float] = None,
        debounce_sec: Optional[float] = None,
        shutdown_callback: Optional[Callable[[], None]] = None,
    ):
        self._project_root = project_root
        self._check_interval = (
            check_interval_sec if check_interval_sec is not None
            else _read_check_interval_sec()
        )
        self._debounce_sec = (
            debounce_sec if debounce_sec is not None
            else _read_debounce_sec()
        )
        self._shutdown_callback = shutdown_callback
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._baseline_hash: Optional[str] = None

    def set_shutdown_callback(self, callback: Callable[[], None]) -> None:
        """陳腐化確定時に呼ばれるコールバックを設定する。"""
        self._shutdown_callback = callback

    def start(self) -> None:
        """ベースラインハッシュを計算し、チェックループスレッドを起動する。

        ``check_interval_sec<=0``（env もしくは明示引数のいずれか経由）の
        場合は無効化する（スレッドを起動しない）。この判定は ``_compute_hash()``
        より前に行う — プロジェクトルート全体を読み切る重い処理を、結果を
        一切使わない無効化ケースでも同期実行してしまうことを避けるため。
        """
        if self._check_interval <= 0:
            logger.info(
                "Staleness watchdog disabled (check_interval_sec<=0), "
                "skipping thread start"
            )
            return
        self._baseline_hash = self._compute_hash()
        self._thread = threading.Thread(
            target=self._check_loop,
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """チェックループスレッドを停止する。"""
        self._stop_event.set()

    def _check_loop(self) -> None:
        """チェックループ本体。

        例外で死んで沈黙するスレッドが最悪の失敗モード（入れてあるのに
        何もしなくなる）なので、1周ごとに例外を捕捉してループを継続する。
        """
        while not self._stop_event.wait(timeout=self._check_interval):
            try:
                self._check_once()
            except Exception:
                logger.exception("staleness check failed, continuing")

    def _check_once(self) -> None:
        """1回分の陳腐化チェック。比較は常に起動時ベースラインに対して行う。"""
        if not self._project_root.exists():
            logger.warning(
                "project root missing, skipping staleness check: %s",
                self._project_root,
            )
            return

        current = self._compute_hash()
        if current == self._baseline_hash:
            return

        logger.info(
            "staleness suspected, rechecking after debounce (%ss)",
            self._debounce_sec,
        )
        if self._stop_event.wait(timeout=self._debounce_sec):
            return  # stop() が呼ばれた

        if not self._project_root.exists():
            logger.warning(
                "project root missing during debounce recheck: %s",
                self._project_root,
            )
            return

        recheck = self._compute_hash()
        if recheck == self._baseline_hash:
            logger.info("staleness false alarm, hash reverted to baseline")
            return

        logger.info("staleness confirmed, triggering shutdown")
        # shutdown確定はワンショット。ここで止めないと、次のcheck_interval経過後も
        # コードが元に戻っていない限り同じベースライン比較で再び確定に達し、
        # shutdown_callback（SIGINT送信）を繰り返し呼んでしまう
        # （SessionManager._grace_timer_workerと同じワンショット設計に揃える）。
        self._stop_event.set()
        if self._shutdown_callback:
            self._shutdown_callback()

    def _compute_hash(self) -> str:
        """project_root配下のファイルcontentハッシュ(sha256)を計算する。

        mtimeベースにしないのは、``uv sync`` やキャッシュの再展開が
        contentを変えずにmtimeだけ触ることがあるため。

        除外ディレクトリは探索前に ``dirs[:]`` のin-place書き換えで刈り込む
        （``Path.rglob()`` で全部歩いてから後filterする実装は `.venv` のような
        巨大ディレクトリを丸ごと探索してしまい非効率）。
        """
        root = self._project_root
        hasher = hashlib.sha256()
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            dirnames.sort()
            pruned = []
            for name in dirnames:
                if name in EXCLUDED_DIR_NAMES:
                    continue
                child_rel = name if rel_dir == "." else os.path.join(rel_dir, name)
                if child_rel in EXCLUDED_RELATIVE_DIRS:
                    continue
                pruned.append(name)
            dirnames[:] = pruned

            for filename in sorted(filenames):
                file_rel = filename if rel_dir == "." else os.path.join(rel_dir, filename)
                file_path = os.path.join(dirpath, filename)
                try:
                    with open(file_path, "rb") as f:
                        content = f.read()
                except OSError:
                    # 探索とハッシュ計算の間にファイルが削除・移動された等、
                    # 一時的なI/Oエラーはこのファイルをスキップして継続する。
                    continue
                hasher.update(file_rel.encode("utf-8", errors="surrogateescape"))
                hasher.update(content)

        return hasher.hexdigest()
