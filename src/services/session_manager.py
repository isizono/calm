"""セッション管理モジュール

HTTPサーバーモードで使用するセッションカウント管理と自動停止ウォッチドッグを提供する。
セッション数が0になると猶予期間後にサーバーをシャットダウンする。
"""
import logging
import os
import sys
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_GRACE_PERIOD_SEC = 30
GRACE_PERIOD_ENV = "CC_MEMORY_AUTO_SHUTDOWN_SEC"


def _read_grace_period_sec() -> int:
    """env から猶予期間を読む。

    未設定・空文字・無効値の場合は ``DEFAULT_GRACE_PERIOD_SEC`` にフォールバックする。
    0 を指定すると auto-shutdown ウォッチドッグを完全に無効化する。

    モジュール外から ``SessionManager()`` 呼び出し時に評価される想定で、
    ``logging.basicConfig`` 未設定でも警告が確実に出るよう ``print`` で stderr に出す。
    """
    raw = os.environ.get(GRACE_PERIOD_ENV)
    if raw is None or raw == "":
        return DEFAULT_GRACE_PERIOD_SEC
    try:
        value = int(raw)
    except ValueError:
        print(
            f"[session_manager] WARNING Invalid {GRACE_PERIOD_ENV}={raw!r}, "
            f"falling back to default {DEFAULT_GRACE_PERIOD_SEC}s",
            file=sys.stderr,
        )
        return DEFAULT_GRACE_PERIOD_SEC
    if value < 0:
        print(
            f"[session_manager] WARNING {GRACE_PERIOD_ENV} must be >= 0, "
            f"got {value}, falling back to default {DEFAULT_GRACE_PERIOD_SEC}s",
            file=sys.stderr,
        )
        return DEFAULT_GRACE_PERIOD_SEC
    return value


class SessionManager:
    """セッションカウント管理 + 自動停止ウォッチドッグ

    スレッドセーフ。register/unregisterはHTTPリクエストハンドラから呼ばれる。
    ウォッチドッグはバックグラウンドスレッドで動作し、セッション0 → 猶予期間 → shutdownを行う。

    ``grace_period_sec`` が ``None`` の場合は env ``CC_MEMORY_AUTO_SHUTDOWN_SEC`` から
    値を読む。env で 0 を指定すると auto-shutdown を完全に無効化し、ウォッチドッグ
    スレッドの起動とシャットダウン発火を一切行わない。
    """

    def __init__(self, grace_period_sec: Optional[int] = None):
        self._active_sessions: set[str] = set()
        self._lock = threading.Lock()
        self._grace_period = (
            grace_period_sec if grace_period_sec is not None
            else _read_grace_period_sec()
        )
        self._shutdown_callback: Optional[Callable[[], None]] = None
        self._shutdown_event = threading.Event()
        self._cancel_event = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None

    @property
    def active_count(self) -> int:
        """アクティブセッション数を返す。"""
        with self._lock:
            return len(self._active_sessions)

    @property
    def session_ids(self) -> set[str]:
        """アクティブセッションIDのコピーを返す。"""
        with self._lock:
            return set(self._active_sessions)

    def register(self, session_id: str) -> bool:
        """セッションを登録する。

        Args:
            session_id: セッション識別子

        Returns:
            新規登録の場合True、既に登録済みの場合False
        """
        with self._lock:
            if session_id in self._active_sessions:
                logger.info(f"Session already registered: {session_id}")
                return False
            self._active_sessions.add(session_id)
            count = len(self._active_sessions)
            # ロック内でキャンセル（_start_grace_timerとのレースコンディション防止）
            self._cancel_event.set()

        logger.info(f"Session registered: {session_id} (active: {count})")
        return True

    def unregister(self, session_id: str) -> bool:
        """セッションを解除する。

        セッション数が0になった場合、猶予期間タイマーを開始する。

        Args:
            session_id: セッション識別子

        Returns:
            解除に成功した場合True、未登録の場合False
        """
        with self._lock:
            if session_id not in self._active_sessions:
                logger.warning(f"Session not found: {session_id}")
                return False
            self._active_sessions.discard(session_id)
            count = len(self._active_sessions)

        logger.info(f"Session unregistered: {session_id} (active: {count})")

        if count == 0:
            self._start_grace_timer()
        return True

    def set_shutdown_callback(self, callback: Callable[[], None]) -> None:
        """シャットダウン時に呼ばれるコールバックを設定する。"""
        self._shutdown_callback = callback

    def start_watchdog(self) -> None:
        """ウォッチドッグスレッドを起動する。

        サーバー起動直後（セッション0の状態）から猶予期間タイマーを開始する。
        """
        self._start_grace_timer()

    @property
    def is_auto_shutdown_disabled(self) -> bool:
        """auto-shutdown が無効化されている (``grace_period_sec=0``) かを返す。"""
        return self._grace_period == 0

    def _start_grace_timer(self) -> None:
        """猶予期間タイマーを（再）開始する。

        ``grace_period_sec=0`` (env もしくは明示引数のいずれか経由) の場合は
        auto-shutdown 完全無効モードとみなしてタイマーを起動しない。
        """
        if self._grace_period == 0:
            logger.info(
                "Auto-shutdown disabled (grace_period_sec=0), "
                "skipping grace timer start"
            )
            return
        with self._lock:
            # 既存のタイマーをキャンセル
            self._cancel_event.set()
            # 新しいイベントに置き換え（ロック内でregisterのset()と競合しない）
            self._cancel_event = threading.Event()
            cancel_event = self._cancel_event

        self._watchdog_thread = threading.Thread(
            target=self._grace_timer_worker,
            args=(cancel_event,),
            daemon=True,
        )
        self._watchdog_thread.start()

    def _grace_timer_worker(self, cancel_event: threading.Event) -> None:
        """猶予期間タイマーのワーカー。

        猶予期間中にcancel_eventがsetされたらタイマーをキャンセルする。
        猶予期間が経過してもセッション0の場合、shutdownコールバックを呼ぶ。
        """
        # 猶予期間待機（cancel_eventがsetされたら早期リターン）
        cancelled = cancel_event.wait(timeout=self._grace_period)
        if cancelled:
            return

        # 猶予期間経過後、セッション数を確認
        with self._lock:
            count = len(self._active_sessions)

        if count == 0:
            logger.info(
                f"No active sessions after {self._grace_period}s grace period, "
                "initiating shutdown"
            )
            self._shutdown_event.set()
            if self._shutdown_callback:
                self._shutdown_callback()
        else:
            logger.info(
                f"Grace period expired but {count} sessions active, "
                "cancelling shutdown"
            )

    @property
    def is_shutdown_requested(self) -> bool:
        """シャットダウンがリクエストされたかどうかを返す。"""
        return self._shutdown_event.is_set()
