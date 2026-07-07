"""セッション管理モジュール

HTTPサーバーモードで使用するセッションカウント管理と自動停止ウォッチドッグを提供する。
セッション数が0になると猶予期間後にサーバーをシャットダウンする。
"""
import logging
import os
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_GRACE_PERIOD_SEC = 30
GRACE_PERIOD_ENV = "CC_MEMORY_AUTO_SHUTDOWN_SEC"

# liveness TTL: heartbeat（register()の再呼び出し）が途絶したsessionを失効させる
LIVENESS_TIMEOUT_ENV = "CC_MEMORY_SESSION_LIVENESS_TIMEOUT_SEC"
DEFAULT_LIVENESS_TIMEOUT_SEC = 300.0
LIVENESS_SWEEP_INTERVAL_SEC = 30.0


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


def _read_liveness_timeout_sec() -> float:
    """env `CC_MEMORY_SESSION_LIVENESS_TIMEOUT_SEC` から liveness TTL を読む。

    未設定・無効値の場合は既定値にフォールバックする。0 を指定すると
    TTL失効を完全に無効化する（reaperスレッドを起動しない）。
    """
    raw = os.environ.get(LIVENESS_TIMEOUT_ENV)
    if raw is None or raw == "":
        return DEFAULT_LIVENESS_TIMEOUT_SEC
    try:
        value = float(raw)
    except ValueError:
        print(
            f"[session_manager] WARNING Invalid {LIVENESS_TIMEOUT_ENV}={raw!r}, "
            f"falling back to default {DEFAULT_LIVENESS_TIMEOUT_SEC}s",
            file=sys.stderr,
        )
        return DEFAULT_LIVENESS_TIMEOUT_SEC
    if value < 0:
        print(
            f"[session_manager] WARNING {LIVENESS_TIMEOUT_ENV} must be >= 0, "
            f"got {value}, falling back to default {DEFAULT_LIVENESS_TIMEOUT_SEC}s",
            file=sys.stderr,
        )
        return DEFAULT_LIVENESS_TIMEOUT_SEC
    return value


class SessionManager:
    """セッションカウント管理 + 自動停止ウォッチドッグ + liveness TTL失効

    スレッドセーフ。register/unregisterはHTTPリクエストハンドラから呼ばれる。
    ウォッチドッグはバックグラウンドスレッドで動作し、セッション0 → 猶予期間 → shutdownを行う。
    register()の再呼び出し（heartbeat）はlast_seenを更新し、一定TTLを超えてheartbeatが
    途絶したsessionはliveness reaperがunregister()と同じ経路で自動的に失効させる。

    ``grace_period_sec`` が ``None`` の場合は env ``CC_MEMORY_AUTO_SHUTDOWN_SEC`` から
    値を読む。env で 0 を指定すると auto-shutdown を完全に無効化し、ウォッチドッグ
    スレッドの起動とシャットダウン発火を一切行わない。

    ``liveness_timeout_sec`` が ``None`` の場合は env
    ``CC_MEMORY_SESSION_LIVENESS_TIMEOUT_SEC`` から値を読む。0 を指定すると
    liveness reaperスレッド自体を起動しない。
    """

    def __init__(
        self,
        grace_period_sec: Optional[int] = None,
        liveness_timeout_sec: Optional[float] = None,
    ):
        self._active_sessions: set[str] = set()
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._grace_period = (
            grace_period_sec if grace_period_sec is not None
            else _read_grace_period_sec()
        )
        self._liveness_timeout = (
            liveness_timeout_sec if liveness_timeout_sec is not None
            else _read_liveness_timeout_sec()
        )
        self._shutdown_callback: Optional[Callable[[], None]] = None
        self._shutdown_event = threading.Event()
        self._cancel_event = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._liveness_stop_event = threading.Event()
        self._liveness_thread: Optional[threading.Thread] = None

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
        """セッションを登録する（heartbeatとしての再送も同じ経路を通る）。

        新規・既存いずれの呼び出しでも last_seen を現在時刻に更新する。

        Args:
            session_id: セッション識別子

        Returns:
            新規登録の場合True、既に登録済みの場合False
        """
        with self._lock:
            is_new = session_id not in self._active_sessions
            self._active_sessions.add(session_id)
            self._last_seen[session_id] = time.monotonic()
            count = len(self._active_sessions)
            if is_new:
                # ロック内でキャンセル（_start_grace_timerとのレースコンディション防止）
                self._cancel_event.set()

        if is_new:
            logger.info(f"Session registered: {session_id} (active: {count})")
        else:
            logger.debug(f"Session heartbeat: {session_id}")
        return is_new

    def unregister(self, session_id: str) -> bool:
        """セッションを解除する。

        セッション数が0になった場合、猶予期間タイマーを開始する。

        Args:
            session_id: セッション識別子

        Returns:
            解除に成功した場合True、未登録の場合False
        """
        with self._lock:
            count = self._remove_session_locked(session_id)

        if count is None:
            logger.warning(f"Session not found: {session_id}")
            return False

        logger.info(f"Session unregistered: {session_id} (active: {count})")

        if count == 0:
            self._start_grace_timer()
        return True

    def _remove_session_locked(self, session_id: str) -> Optional[int]:
        """``self._lock`` 保持中に呼び出すこと。

        セッションを除去し、除去後のアクティブ数を返す。未登録の場合は
        ``None`` を返す（除去は行わない）。
        """
        if session_id not in self._active_sessions:
            return None
        self._active_sessions.discard(session_id)
        self._last_seen.pop(session_id, None)
        return len(self._active_sessions)

    def set_shutdown_callback(self, callback: Callable[[], None]) -> None:
        """シャットダウン時に呼ばれるコールバックを設定する。"""
        self._shutdown_callback = callback

    def start_watchdog(self) -> None:
        """ウォッチドッグスレッド + liveness reaperスレッドを起動する。

        サーバー起動直後（セッション0の状態）から猶予期間タイマーを開始する。
        """
        self._start_grace_timer()
        self._start_liveness_reaper()

    def _start_liveness_reaper(self) -> None:
        """liveness TTL 失効スレッドを起動する。

        `liveness_timeout_sec<=0`（env もしくは明示引数のいずれか経由）の
        場合は無効化する（reaperスレッドを起動しない）。
        """
        if self._liveness_timeout <= 0:
            logger.info(
                "Liveness reaper disabled (liveness_timeout_sec<=0), "
                "skipping reaper thread start"
            )
            return
        self._liveness_thread = threading.Thread(
            target=self._liveness_reaper_worker,
            daemon=True,
        )
        self._liveness_thread.start()

    def _liveness_reaper_worker(self) -> None:
        """last_seen が liveness TTL を超えて更新されていない session を失効させる。

        heartbeat（register() の再呼び出し）が届き続ける限り対象にならない。
        launcher.py が SIGKILL 等で異常終了し heartbeat が止まった場合のみ、
        TTL 経過後に unregister() と同じ経路で active_sessions から外れる。
        """
        while not self._liveness_stop_event.wait(timeout=LIVENESS_SWEEP_INTERVAL_SEC):
            now = time.monotonic()
            with self._lock:
                stale = [
                    sid for sid, last in self._last_seen.items()
                    if now - last > self._liveness_timeout
                ]
            for sid in stale:
                self._evict_if_still_stale(sid)

    def _evict_if_still_stale(self, session_id: str) -> None:
        """stale判定されたsessionを、除去直前に再チェックしてから失効させる。

        stale一覧のスナップショット取得（ロック外）からこの呼び出しまでの間に
        別スレッドから register()（heartbeat）が届くと、その session は既に
        TTL内へ復帰している可能性がある。再チェックと除去を同一ロック区間で
        行うことで、直前に生存申告のあったsessionを誤って失効させることを
        防ぐ（TOCTOU対策）。
        """
        with self._lock:
            last_seen = self._last_seen.get(session_id)
            if last_seen is None:
                return  # 既に unregister 済み
            if time.monotonic() - last_seen <= self._liveness_timeout:
                return  # 再チェック時点では生存（heartbeatが届いていた）
            count = self._remove_session_locked(session_id)

        if count is None:
            return
        logger.warning(f"Session liveness timeout, evicting: {session_id} (active: {count})")
        if count == 0:
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
