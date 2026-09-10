"""askの回答・却下を低遅延で知らせるnotify_pathの生成・書き込み・TTLスイープ。

配置: <notify_dir>/<ask_id>.notify（1行1JSON、answer/dismiss毎に追記）。
relay inbox（src/services/relay/inbox.py）とは独立した仕組みで、identity解決
（resolve_identity_by_ancestry等）には一切依存しない。ファイルは事前生成しない
（answer_ask/triage_ask側が初めて書き込む瞬間に生成する。relay inboxの
precreateパターンで孤児ファイルが蓄積した経緯を、事前生成しないことで避ける）。

通知は「当たれば儲けもの」の位置づけであり、書き込み失敗（ディスク容量等）は
例外を上げずログに残す。呼び出し元（answer_ask/triage_ask）のDB更新の成否とは
独立させる。中身は正ではない。受信側は必ずget_asksで実際の状態を取り直すこと
（ファイルの中身を信用しない）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 古いnotify fileのTTL。中身は一度読まれたら無価値なため、新規書き込み時に
# 同ディレクトリを軽くスイープして削除する（専用cron等の重い仕組みは作らない）。
_TTL_SECONDS = 24 * 60 * 60


def notify_dir() -> Path:
    """notify fileの置き場（env CALM_ASK_NOTIFY_DIR、既定 ~/.cc-memory/asks）。

    relay の state dir（env RELAY_STATE_DIR、既定 ~/.cc-memory/relay）とは
    独立した兄弟ディレクトリ。ask通知はrelayを経由しないため。
    """
    raw = os.environ.get("CALM_ASK_NOTIFY_DIR")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cc-memory" / "asks"


def notify_path(ask_id: int) -> Path:
    """指定ask_idのnotify fileパスを返す（ファイルの存在は問わない）。"""
    return notify_dir() / f"{ask_id}.notify"


def _sweep_expired(dir_path: Path) -> None:
    """TTL（既定24時間）を過ぎたnotify fileを削除する。

    新規書き込み時にのみ同ディレクトリを走査する軽量スイープ。1ファイルの
    削除に失敗しても他のファイルの掃除・本来の書き込み処理は継続する。
    """
    now = time.time()
    try:
        entries = list(dir_path.iterdir())
    except (FileNotFoundError, OSError):
        return
    for entry in entries:
        if not entry.name.endswith(".notify"):
            continue
        try:
            if now - entry.stat().st_mtime > _TTL_SECONDS:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def write_notification(ask_id: int, status: str) -> None:
    """ask_id宛のnotify_pathへ1行追記する（初回呼び出しでファイル生成）。

    書き込み失敗は例外を上げずログに残す（呼び出し元のDB更新をブロックしない）。
    tail -F（--follow=name --retry相当）はファイル出現前からでもretryするため、
    呼び出し元（answer_ask/triage_ask）は事前のfile存在確認を必要としない。
    """
    try:
        dir_path = notify_dir()
        dir_path.mkdir(parents=True, exist_ok=True)
        _sweep_expired(dir_path)
        line = json.dumps(
            {
                "ask_id": ask_id,
                "status": status,
                "at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        )
        with open(notify_path(ask_id), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.warning("ask notify write failed for ask_id=%s", ask_id, exc_info=True)
