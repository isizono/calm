"""relay v2 常駐 3 系統 thread supervisor。

役割:
- B-1（intake, `intake.run`）: SSE 受信 → session inbox 振り分け → ack
- B-2（lease_loop, `lease_loop.run`）: subscription lease renew / resubscribe / 孤児 sweep
- B-3（outbox dispatcher, `relay_sdk.outbox.run_dispatcher`）: `relay_outbox` polling → `POST /publish`

いずれも `daemon=True` の thread で起動し、`threading.Event` で cancel する（既存
`SessionManager` の watchdog と同じ方式）。thread 内で例外が発生した場合は指数
バックオフで再起動する（B-3 は SDK が file lock を持つため、二重起動しても
`DispatcherAlreadyRunning` で自動的に無効化される）。

singleton 担保:
- `start()` は 2 度呼ばれても no-op（プロセス内ガード）。
- `main.py` の http `__main__` ブランチ（`server.lock` 取得後）でのみ起動する
  ことで、cc-memory の local http プロセス（`localhost:52837`）に配置を絞る。
  remote プロセスは `src.main` を import するが `__main__` を実行しないため
  自動的に除外される。
- B-3 は SDK 側の `<db_path>.dispatcher.lock`（fcntl.flock）が第二防壁として
  効く（万一同 process 内で 2 度 spawn しても DispatcherAlreadyRunning を吐く）。

RELAY_BASE_URL / RELAY_BEARER_TOKEN が未設定の環境では 3 系統を全く起動せず、log を
1 行出して静かに終わる（v1 が並走している移行期間で server 起動を壊さないため）。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Callable, Optional, TypedDict

from src.services.relay import config, intake, lease_loop

logger = logging.getLogger(__name__)


class ThreadHealth(TypedDict):
    restart_count: int
    last_restart_at: Optional[str]
    last_error: Optional[str]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- cc-memory 組み込み dispatcher 専用の既定値 -----------------------------------
#
# vendored SDK 自体の既定値（max_retry=5, initial_backoff_seconds=0.1,
# backoff_factor=2.0）は TransientError に対して合計約 1.5 秒で dead 化する。relay
# server は cc-memory とは独立に手動 kill/再起動されるプロセスであり、この秒数を
# 超える再起動断絶は珍しくない。cc-memory 組み込み経路（このモジュール）専用の
# 既定値をここに持つ。
#
# src/relay_sdk/config.py の DEFAULT_* は変更しない。src/relay_sdk/ 配下は
# vendoring 物であり、VENDORED.md が定める「差分は import 書き換えのみ」という
# 再同期不変条件がある。
_EMBEDDED_DEFAULT_MAX_RETRY = 10
_EMBEDDED_DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
_EMBEDDED_DEFAULT_BACKOFF_FACTOR = 2.0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_ms_as_seconds(name: str, default_seconds: float) -> float:
    raw = os.environ.get(name)
    return int(raw) / 1000.0 if raw not in (None, "") else default_seconds


class RelayRuntime:
    """B-1 / B-2 / B-3 を束ねる supervisor（プロセス内シングルトン、二重 start ガード付き）。"""

    def __init__(
        self,
        active_sessions_getter: Callable[[], set[str]],
        *,
        outbox_db_path: Optional[str] = None,
    ) -> None:
        self._active_sessions_getter = active_sessions_getter
        self._outbox_db_path = outbox_db_path
        self._stop_event = threading.Event()
        self._reconfigure_event = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._health: dict[str, ThreadHealth] = {}
        self._health_lock = threading.Lock()
        self._started = False
        self._start_lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------

    @staticmethod
    def is_configured() -> bool:
        """relay 接続に必要な最小設定（token）が揃っているか。"""
        return bool(config.get_token())

    def start(self) -> bool:
        """3 系統 thread を起動する。既に起動済みなら no-op で False を返す。

        RELAY_BEARER_TOKEN 未設定なら thread を起動せず False を返す（server 起動を
        壊さない静かな縮退）。
        """
        with self._start_lock:
            if self._started:
                logger.info("RelayRuntime は既に起動済みです（二重 start を無視）")
                return False
            if not self.is_configured():
                logger.info(
                    "RELAY_BEARER_TOKEN が未設定のため RelayRuntime を起動しません"
                    "（relay v2 未導入環境として扱う）"
                )
                return False
            self._started = True

        self._spawn("relay-intake", self._run_intake)
        self._spawn("relay-lease-loop", self._run_lease_loop)
        self._spawn("relay-dispatcher", self._run_dispatcher)
        logger.info(
            "RelayRuntime started: base_url=%s identity=%s",
            config.get_base_url(),
            config.get_identity(),
        )
        return True

    def stop(self) -> None:
        """全 thread に停止シグナルを送る（daemon なので join は行わない）。"""
        self._stop_event.set()
        self._reconfigure_event.set()

    def notify_reconfigure(self) -> None:
        """intake に「subscription 集合が変わったので再接続してほしい」を伝える。"""
        self._reconfigure_event.set()

    # -- thread supervision -----------------------------------------------

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        thread = threading.Thread(
            target=self._supervise,
            args=(name, target),
            daemon=True,
            name=name,
        )
        with self._health_lock:
            self._health[name] = {
                "restart_count": 0,
                "last_restart_at": None,
                "last_error": None,
            }
            self._threads[name] = thread
        thread.start()

    def _supervise(self, name: str, target: Callable[[], None]) -> None:
        """target を実行し、例外が出たら指数バックオフで再起動する。

        stop_event が set されている間は再起動しない。target が正常終了したら
        supervisor も終了する（正常終了 = 意図した shutdown とみなす）。
        """
        backoff = 1.0
        cap = 30.0
        while not self._stop_event.is_set():
            try:
                target()
                logger.info("%s thread が正常終了しました", name)
                return
            except Exception as exc:
                logger.exception("%s thread が例外で停止（%.1fs 後に再起動）", name, backoff)
                with self._health_lock:
                    # _spawn() を経由せず _supervise() が直接呼ばれるケース（既存テスト等）
                    # では self._health に name が未登録なので setdefault で自己登録する。
                    # _spawn() 経由なら既に登録済みの dict をそのまま返す。
                    health = self._health.setdefault(
                        name,
                        {
                            "restart_count": 0,
                            "last_restart_at": None,
                            "last_error": None,
                        },
                    )
                    health["restart_count"] += 1
                    health["last_restart_at"] = _now_iso()
                    health["last_error"] = f"{type(exc).__name__}: {exc}"
                if self._stop_event.wait(backoff):
                    return
                backoff = min(backoff * 2, cap)

    def health_snapshot(self) -> dict:
        """runtime の稼働状態を返す（relay_status から呼ばれる）。

        thread の生存判定は毎回 `Thread.is_alive()` で問い合わせる（in-memory bool を
        手動管理すると `_supervise` の更新タイミングとズレるため、常に OS 側の実際の
        thread 状態を正とする）。self._health と self._threads は常に同じキー集合を
        持つ（_spawn() が両方を同一ロック区間内で登録するため）ため、1回のロック
        区間内で両方を読み切ってよい。
        """
        with self._health_lock:
            threads = {
                name: {
                    "alive": self._threads[name].is_alive(),
                    "restart_count": info["restart_count"],
                    "last_restart_at": info["last_restart_at"],
                    "last_error": info["last_error"],
                }
                for name, info in self._health.items()
            }
        return {
            "configured": self.is_configured(),
            "running": self._started,
            "threads": threads,
        }

    # -- thread targets ---------------------------------------------------

    def _run_intake(self) -> None:
        intake.run(self._stop_event, self._reconfigure_event)

    def _run_lease_loop(self) -> None:
        lease_loop.run(
            self._stop_event,
            self._active_sessions_getter,
            reconfigure_event=self._reconfigure_event,
        )

    def _run_dispatcher(self) -> None:
        """SDK 付属の `run_dispatcher` を thread で起動する薄い wrapper。

        `RELAY_OUTBOX_MAX_RETRY` / `RELAY_OUTBOX_INITIAL_BACKOFF_MS` /
        `RELAY_OUTBOX_BACKOFF_FACTOR` を明示的に解決して渡す（省略すると SDK 側の
        ハードコード既定値にフォールバックし、これらの env var が一切効かなくなる
        ため）。未設定時のフォールバック先は `_EMBEDDED_DEFAULT_*`（cc-memory 組み込み
        経路専用、SDK 自身の既定値より粘り強い）。

        `poll_interval_seconds` / `dlq_gc_interval_seconds` / `http_timeout_seconds`
        は cc-memory 固有の既定値を持たず、SDK 側の env 解決ヘルパー（`sdk_config.env_*`）
        をそのまま使う。

        二重起動は SDK 側 file lock（`<db_path>.dispatcher.lock`）で拒否される
        （`DispatcherAlreadyRunning`）。dispatcher プロセスは他にもいる（例:
        ユーザーが手動で `python -m relay_sdk.outbox` を起動している）ケースに
        備え、取得失敗はエラーではなく log のみで縮退する。
        """
        from src.relay_sdk import config as sdk_config
        from src.relay_sdk.outbox import DispatcherAlreadyRunning, run_dispatcher

        db_path = self._outbox_db_path
        if db_path is None:
            from src.db import get_db_path

            db_path = get_db_path()
        base_url = config.get_base_url()
        token = config.get_token()
        try:
            run_dispatcher(
                db_path=db_path,
                relay_base_url=base_url,
                bearer_token=token,
                poll_interval_seconds=sdk_config.env_poll_interval_seconds(),
                max_retry=_env_int(
                    "RELAY_OUTBOX_MAX_RETRY", _EMBEDDED_DEFAULT_MAX_RETRY
                ),
                initial_backoff_seconds=_env_ms_as_seconds(
                    "RELAY_OUTBOX_INITIAL_BACKOFF_MS",
                    _EMBEDDED_DEFAULT_INITIAL_BACKOFF_SECONDS,
                ),
                backoff_factor=_env_float(
                    "RELAY_OUTBOX_BACKOFF_FACTOR", _EMBEDDED_DEFAULT_BACKOFF_FACTOR
                ),
                dlq_gc_interval_seconds=sdk_config.env_dlq_gc_interval_seconds(),
                http_timeout_seconds=sdk_config.env_http_timeout_seconds(),
                stop_event=self._stop_event,
            )
        except DispatcherAlreadyRunning as exc:
            logger.info(
                "relay outbox dispatcher は他プロセスが保持中です（縮退）: %s", exc
            )


__all__ = ["RelayRuntime"]
