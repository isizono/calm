"""server 内常駐 B-2: subscription lease の renew / resubscribe / 孤児 sweep。

declaration file を定期スキャンし、以下を実行する:

1. **renew**: lease_expires_at が margin（既定 lease_ttl / 3）を切った entry を PUT lease で
   renew し、declaration file の lease_expires_at を更新する。
2. **resubscribe**: PUT lease が 404 / 410 を返した（relay 側 subscription が失効・消滅）
   場合、同一 labels で新規に POST /subscriptions を叩き、declaration file の
   subscription_id / lease_expires_at を差し替える（relay 再起動からの自己修復）。
3. **孤児 sweep**: 起動時 1 回 + 定期（既定 1 時間毎）。declaration file 内の全
   subscription の lease_expires_at 最大値が閾値（既定 24 時間）以上前なら「誰も
   renew していない退場済み session」とみなし、file と inbox / cursor を一括削除する。
   これとは別に、declaration を経由しない precreate inbox file（SessionStart hook の
   `ensure_inbox_file()`）は declaration ベースの判定に現れないため、inbox dir を
   直接走査し mtime が閾値より古いものを個別に削除する（`compute_orphan_inbox_files`）。

**renew の生存ゲート**: `SessionManager` に登録されていない session_id の
declaration は renew しない。これで死んだ session の lease は自然失効し、孤児
sweep の削除条件が構造的に成立する（renew し続けたら失効しない → sweep されない
の悪循環を防ぐ）。

3 つの動作はすべて時刻・状態を引数で受け取れる純関数として書き、常駐 loop 本体
（`run`）とテストを分離する。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from src.relay_sdk.errors import PermanentError, RelayProtocolError, TransientError
from src.relay_sdk.http.auth import make_client
from src.relay_sdk.http.request import post_subscription, put_lease
from src.services.relay import config, declarations, inbox

logger = logging.getLogger(__name__)


DEFAULT_LEASE_TTL_SECONDS = 300
DEFAULT_RENEW_INTERVAL_SECONDS = 30.0
DEFAULT_SWEEP_INTERVAL_SECONDS = 3600.0
DEFAULT_ORPHAN_THRESHOLD_SECONDS = 24 * 3600  # 24h


# ---------------------------------------------------------------------------
# 純ロジック（時刻・active session 一覧を引数で受ける）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenewAction:
    """renew / resubscribe 判定の 1 結果。"""

    session_id: str
    subscription_id: str
    labels: list[str]
    kind: str  # "renew" | "resubscribe"


def compute_renew_actions(
    snapshot: list[dict],
    *,
    active_session_ids: set[str],
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    now: Optional[datetime] = None,
) -> list[RenewAction]:
    """renew / resubscribe が必要な entry を返す（活き session の分だけ）。

    - `active_session_ids` に含まれない session の entry は返さない（renew しない）。
    - lease_expires_at が現在時刻を過ぎている entry は "resubscribe"。
    - 残り時間が `lease_ttl_seconds / 3` を切っている entry は "renew"。
    - それ以外は返さない（次回スキャンで再判定する）。
    """
    now = now or datetime.now(timezone.utc)
    margin = timedelta(seconds=lease_ttl_seconds / 3)
    actions: list[RenewAction] = []
    for decl in snapshot:
        session_id = decl.get("session_id")
        if not isinstance(session_id, str):
            continue
        if session_id not in active_session_ids:
            continue
        for entry in decl.get("subscriptions", []):
            subscription_id = entry.get("subscription_id")
            labels = list(entry.get("labels", []))
            if not isinstance(subscription_id, str) or not labels:
                continue
            expires = _parse_iso(entry.get("lease_expires_at"))
            if expires is None:
                # 期限不明は resubscribe に倒す（安全側）
                actions.append(
                    RenewAction(
                        session_id=session_id,
                        subscription_id=subscription_id,
                        labels=labels,
                        kind="resubscribe",
                    )
                )
                continue
            remaining = expires - now
            if remaining <= timedelta(0):
                actions.append(
                    RenewAction(
                        session_id=session_id,
                        subscription_id=subscription_id,
                        labels=labels,
                        kind="resubscribe",
                    )
                )
            elif remaining < margin:
                actions.append(
                    RenewAction(
                        session_id=session_id,
                        subscription_id=subscription_id,
                        labels=labels,
                        kind="renew",
                    )
                )
    return actions


def compute_orphan_sessions(
    snapshot: list[dict],
    *,
    now: Optional[datetime] = None,
    threshold_seconds: float = DEFAULT_ORPHAN_THRESHOLD_SECONDS,
) -> list[str]:
    """孤児（誰も renew していない）と判定される session_id 一覧を返す。

    判定基準は「declaration file 内の全 subscription の lease_expires_at 最大値が
    `now - threshold_seconds` より前」。subscription が 1 つも無い declaration file
    もこの条件を満たす（判定可能な最大値がなければ「無限に古い」扱いにする）。
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=threshold_seconds)
    orphans: list[str] = []
    for decl in snapshot:
        session_id = decl.get("session_id")
        if not isinstance(session_id, str):
            continue
        entries = decl.get("subscriptions", [])
        max_expires: Optional[datetime] = None
        for entry in entries:
            expires = _parse_iso(entry.get("lease_expires_at"))
            if expires is None:
                continue
            if max_expires is None or expires > max_expires:
                max_expires = expires
        # 全 entry の期限が cutoff より前 = 全 lease が閾値以上前に失効している。
        # entry ゼロも「判定可能な最大値がない = 無限に古い」で孤児扱いにする。
        if max_expires is None or max_expires < cutoff:
            orphans.append(session_id)
    return orphans


def compute_orphan_inbox_files(
    inbox_files: list[tuple[str, Path]],
    declared_session_ids: set[str],
    *,
    now: Optional[datetime] = None,
    threshold_seconds: float = DEFAULT_ORPHAN_THRESHOLD_SECONDS,
) -> list[tuple[str, Path]]:
    """declaration を経由しない孤児 inbox file を検出する。

    `ensure_inbox_file()` による precreate file は declaration
    （`relay_subscribe`）を経由しないため、declaration ベースの
    `compute_orphan_sessions` の走査対象にならず孤児化する。ここでは
    inbox dir を直接走査した `inbox_files` のうち、`declared_session_ids`
    に無く（= declaration が一度も作られていない）、かつ mtime が
    `now - threshold_seconds` より前のものを孤児と判定する。

    declaration が存在する session の inbox file は compute_orphan_sessions
    側で扱うため、ここでは対象外にする（declared_session_ids に含まれる）。
    """
    now = now or datetime.now(timezone.utc)
    cutoff_ts = (now - timedelta(seconds=threshold_seconds)).timestamp()
    orphans: list[tuple[str, Path]] = []
    for safe_session_id, path in inbox_files:
        if safe_session_id in declared_session_ids:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_ts:
            orphans.append((safe_session_id, path))
    return orphans


def _parse_iso(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# 副作用ラッパ（file 削除 / http 呼び出し）
# ---------------------------------------------------------------------------


def delete_orphan_state(session_id: str) -> None:
    """declaration file と対応する inbox / cursor を削除する。"""
    declarations.delete(session_id)
    for path in (inbox.inbox_path(session_id), inbox.cursor_path(session_id)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("孤児 sweep で削除に失敗: path=%s error=%s", path, exc)


def delete_orphan_inbox_file(safe_session_id: str, path: Path) -> None:
    """declaration を経由しない孤児 inbox file とその cursor を削除する。

    declaration file が無い（= inbox_path()/cursor_path() を非safe
    session_id 経由で引けない）ため、`compute_orphan_inbox_files()` が
    返す path から直接 cursor path を導出する（拡張子だけ違う命名規則、
    `inbox.cursor_path()` と同じ）。
    """
    for target in (path, path.with_suffix(".cursor")):
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("孤児 inbox file の削除に失敗: path=%s error=%s", target, exc)


def apply_action(
    action: RenewAction,
    client,
    *,
    lease_ttl_seconds: int,
    subscriber_identity: str,
    now_iso_fn: Callable[[], str] = declarations.now_iso,
    reconfigure_event: Optional[threading.Event] = None,
) -> None:
    """1 件の RenewAction を relay に反映して declaration file を更新する。

    renew の 404 / 410 は relay 側 subscription 失効 → resubscribe に自動格下げ。
    resubscribe 成功時は subscription_id が差し替わるため、intake（B-1）へ
    reconfigure_event を通知する。
    """
    decl = declarations.load(action.session_id)
    if decl is None:
        # sweep 済みなど、宣言が消えているケース。処理せず終了する。
        return
    entry = _find_entry(decl, action.subscription_id)
    if entry is None:
        return

    if action.kind == "renew":
        try:
            result = put_lease(
                client,
                subscription_id=action.subscription_id,
                lease_ttl=lease_ttl_seconds,
            )
            entry["lease_expires_at"] = result["lease_expires_at"]
            declarations.save(decl)
            return
        except PermanentError:
            # relay 側の subscription が消えている（再起動など）→ resubscribe に切替
            pass
        except TransientError as exc:
            logger.info("lease renew transient failure（次回再試行）: %s", exc)
            return
        except RelayProtocolError as exc:
            logger.warning("lease renew でプロトコルエラー: %s", exc)
            return

    # resubscribe（renew の PermanentError からのフォールスルー含む）
    try:
        created = post_subscription(
            client,
            subscriber=subscriber_identity,
            labels=sorted(set(action.labels)),
            lease_ttl=lease_ttl_seconds,
        )
    except (TransientError, PermanentError, RelayProtocolError) as exc:
        logger.info("resubscribe transient/protocol failure（次回再試行）: %s", exc)
        return

    entry["subscription_id"] = created["subscription_id"]
    entry["lease_expires_at"] = created.get("lease_expires_at")
    entry["created_at"] = now_iso_fn()
    declarations.save(decl)
    if reconfigure_event is not None:
        reconfigure_event.set()


def _find_entry(decl: dict, subscription_id: str) -> Optional[dict]:
    for entry in decl.get("subscriptions", []):
        if entry.get("subscription_id") == subscription_id:
            return entry
    return None


# ---------------------------------------------------------------------------
# 常駐 loop
# ---------------------------------------------------------------------------


class ActiveSessionSource:
    """SessionManager に触れる部分を抽象化（テスト差し替え用の型）。"""

    def __call__(self) -> set[str]:  # pragma: no cover - protocol
        raise NotImplementedError


def run(
    stop_event: threading.Event,
    active_sessions_getter: Callable[[], set[str]],
    reconfigure_event: Optional[threading.Event] = None,
    *,
    renew_interval_seconds: float = DEFAULT_RENEW_INTERVAL_SECONDS,
    sweep_interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
    orphan_threshold_seconds: float = DEFAULT_ORPHAN_THRESHOLD_SECONDS,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> None:
    """B-2 常駐 loop。stop_event が set されるまで renew / sweep を回す。"""
    identity = config.get_identity()
    base_url = config.get_base_url()
    token = config.get_token()

    # 起動時に 1 回 sweep を実行しておく（再起動直後の孤児掃除）。
    _sweep_orphans(orphan_threshold_seconds)
    last_sweep_monotonic = _monotonic()

    while not stop_event.is_set():
        try:
            active = active_sessions_getter()
        except Exception:
            logger.exception("active session 取得に失敗（今回スキャンを skip）")
            active = set()

        snapshot = declarations.load_all()
        actions = compute_renew_actions(
            snapshot,
            active_session_ids=active,
            lease_ttl_seconds=lease_ttl_seconds,
        )
        if actions:
            try:
                with make_client(base_url, bearer_token=token) as client:
                    for action in actions:
                        apply_action(
                            action,
                            client,
                            lease_ttl_seconds=lease_ttl_seconds,
                            subscriber_identity=identity,
                            reconfigure_event=reconfigure_event,
                        )
            except Exception:
                logger.exception("lease_loop: renew/resubscribe 中に例外（次回再試行）")

        if _monotonic() - last_sweep_monotonic >= sweep_interval_seconds:
            _sweep_orphans(orphan_threshold_seconds)
            last_sweep_monotonic = _monotonic()

        if stop_event.wait(renew_interval_seconds):
            return


def _sweep_orphans(threshold_seconds: float) -> None:
    snapshot = declarations.load_all()
    orphans = compute_orphan_sessions(snapshot, threshold_seconds=threshold_seconds)
    for session_id in orphans:
        logger.info("孤児 declaration を削除: session=%s", session_id)
        delete_orphan_state(session_id)

    # declaration を経由しない precreate inbox file（ensure_inbox_file）の孤児掃除。
    # declaration が存在する session の inbox file は上記の declaration ベース sweep
    # に既に含まれるため、ここでは declared_session_ids で除外する。
    declared_session_ids = declarations.list_declared_session_ids()
    inbox_orphans = compute_orphan_inbox_files(
        inbox.list_inbox_files(),
        declared_session_ids,
        threshold_seconds=threshold_seconds,
    )
    for safe_session_id, path in inbox_orphans:
        logger.info("孤児 precreate inbox file を削除: session=%s", safe_session_id)
        delete_orphan_inbox_file(safe_session_id, path)


def _monotonic() -> float:
    import time

    return time.monotonic()


__all__ = [
    "RenewAction",
    "apply_action",
    "compute_orphan_inbox_files",
    "compute_orphan_sessions",
    "compute_renew_actions",
    "delete_orphan_inbox_file",
    "delete_orphan_state",
    "run",
]
