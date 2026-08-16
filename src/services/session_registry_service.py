"""稼働中 Claude Code セッションの「CLI表示名 → 人間可読な別名」対応表。

配置: ``~/.cc-memory/session_aliases.json``（env ``CCM_SESSION_REGISTRY_PATH``
で差し替え可）。DBマイグレーションを要さない揮発性データとして意図的に
ファイル保持する。cc-memory server はローカル/リモードの複数プロセスで
稼働しうり、hookのような別プロセスからもMCP往復なしに読めることを優先した
選択であり、in-memory辞書は採らない。

キーは CLI の ``sessionId``（cli_session_id）であって表示名（name）ではない。
name はユーザーが CLI 側でリネームすると変わる可変フィールドであり、キーに
すると旧名の行が残り続けるため。name は呼び出しのたびに
``src.infra.cli_session.read_cli_session`` から取り直して最新化する。

read-modify-write は data file 自体ではなく専用の lock file
（``~/.cc-memory/session_aliases.lock``）を flock する。data file は
tmp→``os.replace`` で更新するため inode が入れ替わり、data file 自体を
flock すると待機中のプロセスが unlink 済み inode のロックを握ったまま
通過してしまう。
"""
from __future__ import annotations

import fcntl
import itertools
import json
import os
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from src.infra import cli_session
from src.infra.lock_file import is_process_alive
from src.services.relay import identity as relay_identity

REGISTRY_PATH_ENV = "CCM_SESSION_REGISTRY_PATH"

ALIAS_MAX_CHARS = 24
_MAX_ENTRIES = 64
_TTL_DAYS = 7

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def registry_path() -> Path:
    """対応表ファイルのパス（既定 ``~/.cc-memory/session_aliases.json``）。"""
    raw = os.environ.get(REGISTRY_PATH_ENV)
    return Path(raw).expanduser() if raw else Path.home() / ".cc-memory" / "session_aliases.json"


def _lock_path() -> Path:
    return registry_path().with_suffix(".lock")


@contextmanager
def _locked() -> Iterator[None]:
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    """呼び出し前に ``_locked()`` を保持していること。壊れた/想定外の中身は空で返す。"""
    path = registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "sessions": {}}
    if not isinstance(data, dict):
        return {"version": 1, "sessions": {}}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    return {"version": 1, "sessions": sessions}


def _save(data: dict) -> None:
    """呼び出し前に ``_locked()`` を保持していること。tmp file → atomic rename。"""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def derive_alias(activity_title: str, activity_id: int) -> str:
    """activity タイトルから表示用の別名を作る。

    先頭の ``[議論]`` / ``[作業]`` 等の区分プレフィックスは残す（何のフェーズの
    作業かは表示価値が高いため、意図的に削らない）。24文字を超える場合は
    末尾を省略記号（…）に置き換える。
    """
    s = unicodedata.normalize("NFKC", activity_title or "")
    s = _CONTROL_CHARS_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return f"activity-{activity_id}"
    if len(s) > ALIAS_MAX_CHARS:
        s = s[:ALIAS_MAX_CHARS].rstrip() + "…"
    return s


def _resolve_collision(sessions: dict, base: str, self_key: str) -> tuple[str, bool]:
    """base が他セッションの alias と衝突するなら ``-2``, ``-3`` … を付す。

    自分自身（``self_key``）の既存 alias は taken から除く。除かないと、同じ
    activity へ再 check_in するたびに自分の alias が -2, -3, -4 と際限なく
    ずれていく（前回書き込んだ自分のalias自身に「衝突」する）。
    """
    taken = {
        e.get("alias")
        for k, e in sessions.items()
        if k != self_key and isinstance(e, dict) and e.get("alias")
    }
    if base not in taken:
        return base, False
    for n in itertools.count(2):
        cand = f"{base}-{n}"
        if cand not in taken:
            return cand, True


def _entry_alive(cli_session_id: str, entry: dict, now: datetime) -> bool:
    """行が生存・非stale と判定できるか（cli_pid生存・PID再利用でない・TTL内）。"""
    if not isinstance(entry, dict):
        return False
    pid = entry.get("cli_pid")
    if not isinstance(pid, int) or not is_process_alive(pid):
        return False
    session = cli_session.read_cli_session(pid)
    if session is None or session.get("cli_session_id") != cli_session_id:
        return False
    updated_at = entry.get("updated_at")
    if not isinstance(updated_at, str):
        return False
    try:
        ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now - ts > timedelta(days=_TTL_DAYS):
        return False
    return True


def _gc(sessions: dict) -> None:
    """生存していない行・TTL超過行を削除し、上限超過分を最古から削除する（in-place）。"""
    now = datetime.now(timezone.utc)
    for key in [k for k, e in sessions.items() if not _entry_alive(k, e, now)]:
        del sessions[key]
    overflow = len(sessions) - _MAX_ENTRIES
    if overflow > 0:
        ordered = sorted(sessions.items(), key=lambda kv: kv[1].get("updated_at") or "")
        for key, _ in ordered[:overflow]:
            del sessions[key]


def register_checkin(
    *,
    bridge_session_id: Optional[str],
    activity_id: int,
    activity_title: str,
    activity_status: str,
) -> Optional[dict]:
    """check_in 時に呼ばれ、呼び出し元セッションの行を作成・更新する。

    CLI を解決できない（launcher 登録不在・CLI session file 不在・非CLIクライアント
    からの呼び出し等）場合は None を返す。呼び出し側はこれを理由に check_in 自体を
    失敗させてはならない。

    別の activity へ check_in し直すと、以前 set_alias で付けた手動 alias は
    破棄され、新しい activity から自動生成した alias に戻る（本関数の仕様）。
    """
    if not bridge_session_id:
        return None
    cli = relay_identity.resolve_cli_session(bridge_session_id)
    if cli is None:
        return None
    cli_session_id = cli.get("cli_session_id")
    if not cli_session_id:
        return None

    with _locked():
        data = _load()
        sessions = data["sessions"]
        existing = sessions.get(cli_session_id)
        keep_manual = (
            isinstance(existing, dict)
            and existing.get("alias_source") == "manual"
            and existing.get("alias_activity_id") == activity_id
        )
        if keep_manual:
            alias = existing["alias"]
            alias_source = "manual"
            collided = False
        else:
            base = derive_alias(activity_title, activity_id)
            alias, collided = _resolve_collision(sessions, base, cli_session_id)
            alias_source = "derived"

        sessions[cli_session_id] = {
            "name": cli["name"],
            "alias": alias,
            "alias_source": alias_source,
            "alias_activity_id": activity_id,
            "activity_title": activity_title,
            "activity_status": activity_status,
            "cli_pid": cli["cli_pid"],
            "cwd": cli.get("cwd"),
            "bridge_session_id": bridge_session_id,
            "updated_at": _now_iso(),
        }
        _gc(sessions)
        _save(data)

    return {"name": cli["name"], "alias": alias, "collided": collided}


def list_sessions(*, self_bridge_session_id: Optional[str] = None) -> list[dict]:
    """稼働中セッションの一覧を updated_at 降順で返す。

    呼び出し元自身の行（self_bridge_session_id から解決できた場合）は
    ``is_self: True`` を持つ。
    """
    self_cli_session_id: Optional[str] = None
    if self_bridge_session_id:
        self_cli = relay_identity.resolve_cli_session(self_bridge_session_id)
        if self_cli is not None:
            self_cli_session_id = self_cli.get("cli_session_id")

    with _locked():
        data = _load()
        sessions = data["sessions"]
        _gc(sessions)
        _save(data)
        items = [
            {
                "name": entry.get("name"),
                "alias": entry.get("alias"),
                "alias_source": entry.get("alias_source", "derived"),
                "activity_id": entry.get("alias_activity_id"),
                "activity_title": entry.get("activity_title"),
                "activity_status": entry.get("activity_status"),
                "cwd": entry.get("cwd"),
                "is_self": key == self_cli_session_id,
                "updated_at": entry.get("updated_at"),
            }
            for key, entry in sessions.items()
        ]

    items.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    return items


def _normalize_manual_alias(alias: str) -> Optional[str]:
    """set_alias 用の手動 alias 検証。改行・制御文字を含む、または前後空白を
    除いて1〜24文字の範囲外なら None（呼び出し側は VALIDATION_ERROR にする）。
    """
    if not isinstance(alias, str) or _CONTROL_CHARS_RE.search(alias):
        return None
    stripped = alias.strip()
    if not stripped or len(stripped) > ALIAS_MAX_CHARS:
        return None
    return stripped


def set_alias(*, bridge_session_id: Optional[str], alias: str) -> dict:
    """自セッションの alias を手動で上書きする。"""
    normalized = _normalize_manual_alias(alias)
    if normalized is None:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "alias は1〜24文字、改行・制御文字は不可です",
            }
        }

    if not bridge_session_id:
        return {
            "error": {
                "code": "SESSION_UNRESOLVED",
                "message": "呼び出し元セッションを識別できませんでした",
            }
        }
    cli = relay_identity.resolve_cli_session(bridge_session_id)
    if cli is None or not cli.get("cli_session_id"):
        return {
            "error": {
                "code": "SESSION_UNRESOLVED",
                "message": "呼び出し元の Claude Code CLI プロセスを解決できませんでした",
            }
        }
    cli_session_id = cli["cli_session_id"]

    with _locked():
        data = _load()
        sessions = data["sessions"]
        existing = sessions.get(cli_session_id)
        if not isinstance(existing, dict):
            return {
                "error": {
                    "code": "NOT_REGISTERED",
                    "message": "先に check_in で対象アクティビティに着手してください",
                }
            }
        alias, collided = _resolve_collision(sessions, normalized, cli_session_id)
        existing["name"] = cli["name"]
        existing["alias"] = alias
        existing["alias_source"] = "manual"
        existing["cwd"] = cli.get("cwd")
        existing["updated_at"] = _now_iso()
        _gc(sessions)
        _save(data)

    return {"name": cli["name"], "alias": alias, "requested_alias": normalized, "collided": collided}


__all__ = [
    "REGISTRY_PATH_ENV",
    "ALIAS_MAX_CHARS",
    "registry_path",
    "derive_alias",
    "register_checkin",
    "list_sessions",
    "set_alias",
]
