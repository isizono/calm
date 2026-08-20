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
          "created_at": str,         # ISO8601 UTC
          "handle_auto_attached": bool  # optional。relay_subscribe が新規
                                         # entry 作成時に必ず False を書き込む
                                         # （service.py）。旧コード（handle 自動
                                         # 付与あり）が書いた entry にはこの
                                         # キー自体が無い。normalize_all_declarations
                                         # はこのキーが無い entry のみを正規化対象
                                         # にする（詳細は同関数の docstring 参照）
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

# entry がこのキーを持つ（値は常に False）ことは、relay_subscribe の handle
# 自動付与廃止後のコードが新規作成した entry であることを構造的に保証する
# （service.py の relay_subscribe が新規 entry 作成時に必ず書き込む。既存
# entry を返すだけの reuse パスやlease_loopのrenew/resubscribeでは付与し
# ないが、それらは元々このキーを持っていた entry を書き換えるだけなので
# 一度付いたキーは保持され続ける）。このキーを持つ entry の labels に自
# handle が混入していても、それは「宛先を自分に限定した複合条件」という
# 本 PR が推奨する意図的な指定でしかあり得ない（自動付与コードはもう
# 存在しないため）。normalize はこのキーが無い entry のみを対象にする。
_HANDLE_AUTO_ATTACHED_KEY = "handle_auto_attached"


def _parse_lease_expires_at(entry: dict) -> Optional[datetime]:
    raw = entry.get("lease_expires_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalize_all_declarations() -> int:
    """旧形式（話題 labels に自 handle が混入した購読）の declaration を正規化する。

    relay_subscribe の handle 自動付与廃止に伴う移行処理。常駐ループ（intake /
    lease_loop）の起動前に 1 回呼ぶ想定。各 entry について、`_HANDLE_AUTO_ATTACHED_KEY`
    を持たない（＝旧コードが作った可能性がある）entry のうち、labels に自 handle
    （`handle:<declaration の handle>`）が含まれ、かつ他の label もある場合のみ
    自 handle を除去する。`_HANDLE_AUTO_ATTACHED_KEY` を持つ entry（新コードが
    作成済み）は、labels の中身に関わらず絶対に触らない。これは本 PR が
    docstring・仕様書で「宛先を自分に限定した複合条件を張りたい場合は labels に
    自分の handle label を明示的に含めること」と案内している新しい意図的な
    使い方を、移行処理が「旧バグの残骸」と誤認して破壊しないようにするため
    （handle 単独 entry・他セッションの handle を含む複合 entry も従来どおり
    対象外）。

    除去の結果 labels 集合が別 entry と衝突した場合、以下の優先順で 1 件だけ
    残し、他を落とす:
      1. 今回の呼び出しで strip されなかった（＝既に健全な）entry を優先
      2. `lease_expires_at` がより未来（＝より最近 renew された）entry を優先
      3. 出現順（決定的なタイブレーク）
    「衝突した2entryのうち出現順が早いほうを無条件で残す」と、たまたま健全な
    entry が後に見つかっただけで消えてしまう（renew され続けてきた生きた
    subscription が失われる）ため、この優先順で判定する。

    書き換えた entry は `lease_expires_at` を現在時刻に設定し（削除はしない。
    全 entry の期限が不明だと孤児 sweep が declaration ごと即削除してしまう
    ため）、lease_loop の renew/resubscribe 判定に「期限切れ→resubscribe」
    として乗せ、新 labels での再購読へつなげる。

    正規化後の entry は「自 handle ＋ 他 label」の形を持たないため、再実行しても
    no-op（冪等）。戻り値は書き換えた declaration の件数。
    """
    changed_count = 0
    for decl in load_all():
        handle_label = f"{_HANDLE_PREFIX}{decl.get('handle', '')}"
        changed = False

        # labels 集合（strip 後）をキーに entry をグルーピングする。order は
        # 出現順を保持し、辞書の反復順（Python 3.7+ で挿入順）に依存しない
        # 明示的な決定性を持たせる。
        groups: dict[frozenset, list[tuple[dict, bool, set]]] = {}
        order: list[frozenset] = []

        for entry in decl.get("subscriptions", []):
            labels = set(entry.get("labels", []))
            stripped = False
            if _HANDLE_AUTO_ATTACHED_KEY not in entry:
                if handle_label in labels and len(labels) > 1:
                    labels.discard(handle_label)
                    stripped = True
            key = frozenset(labels)
            bucket = groups.setdefault(key, [])
            if not bucket:
                order.append(key)
            bucket.append((entry, stripped, labels))

        kept: list[dict] = []
        for key in order:
            bucket = groups[key]
            if len(bucket) > 1:
                changed = True
                # stripped=False（健全）を優先し、次点で lease_expires_at が
                # より未来のものを優先する。datetime 比較不能（None）は
                # 最も不利に扱う。
                def _priority(item: tuple[dict, bool, set]) -> tuple[bool, float]:
                    item_entry, item_stripped, _labels = item
                    expires = _parse_lease_expires_at(item_entry)
                    return (
                        item_stripped,
                        -(expires.timestamp() if expires is not None else float("-inf")),
                    )

                bucket = sorted(bucket, key=_priority)

            winner_entry, winner_stripped, winner_labels = bucket[0]
            if winner_stripped:
                winner_entry["labels"] = sorted(winner_labels)
                winner_entry["lease_expires_at"] = now_iso()
                changed = True
            kept.append(winner_entry)

        if changed:
            decl["subscriptions"] = kept
            save(decl)
            changed_count += 1
    return changed_count
