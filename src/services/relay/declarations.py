"""subscription declaration file（session 単位の購読宣言）の read/write。

配置: <state_dir>/subscriptions/session-<session_id>.json（1 session = 1 file）。
file 存在 = 購読 active、削除 = 退場、を意味する。スキーマ:

    {
      "session_id": str,
      "handle": str,
      "subscriptions": [
        {
          "subscription_id": str,
          "labels": [str],
          "lease_expires_at": str,   # ISO8601 UTC
          "created_at": str          # ISO8601 UTC
        },
        ...
      ]
    }

labels の同一性は集合として比較する（順序・重複の違いは同一宣言とみなす）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.services.relay import config

logger = logging.getLogger(__name__)

_HANDLE_ID_CHARS = 8
_SAFE_SESSION_ID = re.compile(r"[^A-Za-z0-9._-]")


def _safe_session_id(session_id: str) -> str:
    """session_id をファイル名に安全な形へ正規化する（パス区切り等を除去）。"""
    return _SAFE_SESSION_ID.sub("_", session_id)


def declaration_path(session_id: str) -> Path:
    return config.subscriptions_dir() / f"session-{_safe_session_id(session_id)}.json"


def generate_handle(session_id: str) -> str:
    """session の handle を session_id の短縮形から決定的に生成する。"""
    compact = _SAFE_SESSION_ID.sub("", session_id).replace("-", "").replace("_", "").lower()
    short = compact[:_HANDLE_ID_CHARS] or _safe_session_id(session_id).lower()
    return f"session-{short}"


def list_declared_session_ids() -> set[str]:
    """subscriptions dir 配下に declaration file が存在する session の
    safe session_id（ファイル名から抽出、_safe_session_id適用後の形）集合を返す。

    declaration の中身（JSON）は読まず、ファイル名一覧のみを見る軽量版。
    inbox file 側（safe_session_id ベース）との突き合わせ用。
    """
    subs_dir = config.subscriptions_dir()
    if not subs_dir.exists():
        return set()
    return {
        path.name[len("session-") : -len(".json")]
        for path in subs_dir.iterdir()
        if path.name.startswith("session-") and path.name.endswith(".json")
    }


def load_all() -> list[dict]:
    """subscriptions dir 配下の全 declaration file を読み込んで返す。

    壊れた JSON / dict 以外の file は skip する（スキャンを止めない）。
    file 順序は listdir 順に依存する（呼び出し側で必要なら sort する）。
    """
    subs_dir = config.subscriptions_dir()
    if not subs_dir.exists():
        return []
    result: list[dict] = []
    for path in sorted(subs_dir.iterdir()):
        if not (path.name.startswith("session-") and path.name.endswith(".json")):
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            decl = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(decl, dict):
            continue
        decl.setdefault("subscriptions", [])
        result.append(decl)
    return result


def delete(session_id: str) -> bool:
    """declaration file を削除する。存在しなければ False。"""
    path = declaration_path(session_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def load(session_id: str) -> Optional[dict]:
    """declaration file を読み込む。不在・壊れた JSON は None（新規作成扱い）。"""
    path = declaration_path(session_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        decl = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("declaration file が JSON として読めません: %s", path)
        return None
    if not isinstance(decl, dict):
        return None
    decl.setdefault("subscriptions", [])
    return decl


def save(decl: dict) -> Path:
    """declaration file を atomic に書き込む（tmp file → rename）。"""
    path = declaration_path(decl["session_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(decl, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def ensure(session_id: str) -> dict:
    """session の declaration を返す。不在なら handle を生成して新規作成・保存する。"""
    decl = load(session_id)
    if decl is not None and decl.get("handle"):
        return decl
    decl = {
        "session_id": session_id,
        "handle": generate_handle(session_id),
        "subscriptions": [],
    }
    save(decl)
    return decl


def find_subscription(decl: dict, labels: list[str]) -> Optional[dict]:
    """同一 labels 集合の subscription entry を返す。無ければ None。"""
    target = set(labels)
    for entry in decl.get("subscriptions", []):
        if set(entry.get("labels", [])) == target:
            return entry
    return None


def upsert_subscription(decl: dict, entry: dict) -> None:
    """同一 labels 集合の entry を差し替える（無ければ追加する）。"""
    target = set(entry.get("labels", []))
    subscriptions = decl.setdefault("subscriptions", [])
    for i, existing in enumerate(subscriptions):
        if set(existing.get("labels", [])) == target:
            subscriptions[i] = entry
            return
    subscriptions.append(entry)


def lease_active(entry: dict, now: Optional[datetime] = None) -> bool:
    """lease_expires_at が現在より未来なら True。欠落・parse 不能は False（不明扱い）。"""
    raw = entry.get("lease_expires_at")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return expires > now


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# service.py の _HANDLE_PREFIX と同じ値（循環 import を避けるためここでは定数を
# 独立して持つ）。
_HANDLE_PREFIX = "handle:"


def normalize_all_declarations() -> int:
    """旧形式（話題 labels に自 handle が混入した購読）の declaration を正規化する。

    relay_subscribe の handle 自動付与廃止に伴う移行処理。常駐ループ（intake /
    lease_loop）の起動前に 1 回呼ぶ想定。各 entry について、labels に自 handle
    （`handle:<declaration の handle>`）が含まれ、かつ他の label もある場合のみ
    自 handle を除去する。handle 単独 entry（直接メッセージ購読）や他セッションの
    handle を含む複合 entry（意図的な指定でありうる）は触らない。

    除去の結果 labels 集合が別 entry と重複した場合は片方を落とす。書き換えた
    entry は `lease_expires_at` を現在時刻に設定し（削除はしない。全 entry の
    期限が不明だと孤児 sweep が declaration ごと即削除してしまうため）、
    lease_loop の renew/resubscribe 判定に「期限切れ→resubscribe」として乗せ、
    新 labels での再購読へつなげる。

    正規化後の entry は「自 handle ＋ 他 label」の形を持たないため、再実行しても
    no-op（冪等）。戻り値は書き換えた declaration の件数。
    """
    changed_count = 0
    for decl in load_all():
        handle_label = f"{_HANDLE_PREFIX}{decl.get('handle', '')}"
        changed = False
        seen: set[frozenset] = set()
        kept: list[dict] = []
        for entry in decl.get("subscriptions", []):
            labels = set(entry.get("labels", []))
            if handle_label in labels and len(labels) > 1:
                labels.discard(handle_label)
                entry["labels"] = sorted(labels)
                entry["lease_expires_at"] = now_iso()
                changed = True
            key = frozenset(labels)
            if key in seen:
                changed = True
                continue
            seen.add(key)
            kept.append(entry)
        if changed:
            decl["subscriptions"] = kept
            save(decl)
            changed_count += 1
    return changed_count
