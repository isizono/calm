"""per-session inbox（JSONL）の append / drain / cursor 管理。

配置: <state_dir>/inbox/session-<session_id>.jsonl（1 行 = 1 メッセージの JSON object）。
cursor は「どこまで読んだか」のバイトオフセットで、対になる .cursor ファイルに保持する。

配達契約は at-least-once。cursor の欠落・巻き戻りは「重複して返す」側に倒す
（取りこぼす側には倒さない）。append と drain は inbox ファイルの flock で相互排他し、
別プロセスの書き手（server 側 intake）と安全に共存する。

count_unread() は drain() と同じファイルを読むが、cursor は前進させない
（peek専用）。SessionStart hook 等、実際に受信するかどうかをまだ決めていない
呼び出し元のための軽量な件数確認手段。
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Optional

from src.services.relay import config
from src.services.relay.declarations import _safe_session_id

logger = logging.getLogger(__name__)


def inbox_path(session_id: str) -> Path:
    return config.inbox_dir() / f"session-{_safe_session_id(session_id)}.jsonl"


def cursor_path(session_id: str) -> Path:
    return config.inbox_dir() / f"session-{_safe_session_id(session_id)}.cursor"


def read_cursor(session_id: str) -> int:
    """cursor（読了済みバイトオフセット）を返す。欠落・不正は 0（先頭から読み直し）。"""
    try:
        raw = cursor_path(session_id).read_text(encoding="utf-8").strip()
        value = int(raw)
        return value if value >= 0 else 0
    except (FileNotFoundError, ValueError):
        return 0


def _write_cursor(session_id: str, offset: int) -> None:
    path = cursor_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".cursor.tmp")
    tmp.write_text(str(offset), encoding="utf-8")
    os.replace(tmp, path)


def append(session_id: str, record: dict) -> None:
    """inbox に 1 レコードを追記する（flock 排他 + fsync）。"""
    path = inbox_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "ab") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def drain(session_id: str, limit: Optional[int] = None) -> list[dict]:
    """cursor 位置から未読レコードを読み出して返し、cursor を前進させる。

    - inbox file 不在（未配達）は空リスト（エラーにしない）
    - 改行で終端していない末尾の書きかけ行は消費しない（次回 drain に持ち越す）
    - JSON として読めない行はスキップして先へ進む（1 行の破損で inbox 全体を殺さない）
    - 末尾まで読み切ったら file を切り詰めて cursor を 0 に戻す（inbox の肥大防止）
    """
    path = inbox_path(session_id)
    try:
        f = open(path, "r+b")
    except FileNotFoundError:
        return []

    records: list[dict] = []
    with f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            size = os.fstat(f.fileno()).st_size
            offset = read_cursor(session_id)
            if offset > size:
                # file が外部で切り詰められた等の不整合は先頭から読み直す
                # （at-least-once: 取りこぼしより重複を選ぶ）
                offset = 0
            f.seek(offset)
            while True:
                if limit is not None and len(records) >= limit:
                    break
                line_start = f.tell()
                line = f.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    # 書きかけ行: 消費せず次回に持ち越す
                    f.seek(line_start)
                    break
                try:
                    record = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.warning(
                        "inbox の 1 行が JSON として読めないためスキップします: %s", path
                    )
                    continue
                if isinstance(record, dict):
                    records.append(record)
            new_offset = f.tell()
            if new_offset >= size:
                f.truncate(0)
                _write_cursor(session_id, 0)
            else:
                _write_cursor(session_id, new_offset)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return records


def count_unread(session_id: str) -> int:
    """未読メッセージ数を非破壊に数える（drain()と異なりcursorを前進させない）。

    inbox file 不在、または cursor が末尾に達している場合は 0。
    末尾の改行未達（書きかけ）行は数えない。flock(LOCK_SH) で読み取り中の
    append() と排他し、書きかけの途中状態を読まないようにする。
    """
    path = inbox_path(session_id)
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return 0
    with f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            size = os.fstat(f.fileno()).st_size
            offset = read_cursor(session_id)
            if offset > size:
                # drain() と同じ規約: 不整合時は先頭からとみなす
                offset = 0
            if offset >= size:
                return 0
            f.seek(offset)
            tail = f.read()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return tail.count(b"\n")
