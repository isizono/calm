"""MessageDisplay hook: assistant 発話 chunk の内部 ID リテラル
(`[MDLAT]#NNN` または英語フルワード `log/decision/activity/material/topic #NNN`)
の直後にエンティティタイトルを差し込んで表示する。

`hookSpecificOutput.displayContent` で表示のみ書き換える。transcript と
Claude context には元のテキストが残るため、AI 側の token 消費は変わらない。
ユーザー画面でだけ可読性が上がる。

MessageDisplay は実機では `delta` + `index` + `final` + `message_id` の
streaming protocol で発火する。本 hook は chunk 単位 (`delta`) で補完を行い、
chunk 境界をまたぐ ID リテラル (例: chunk N が `M#`、chunk N+1 が `123 と`)
は補完漏れになる trade-off を受け入れる。`final=true` 待ち全文累積モデルは
今回採用しない。

cc-memory project 内かどうかは判定せず、全 session で有効。
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from src.services.internal_id_patterns import (  # noqa: E402
    FULLWORD_TO_CODE,
    RAW_CITE_CODE_PATTERN,
    RAW_CITE_FULLWORD_PATTERN,
)

DEFAULT_DB_PATH = pathlib.Path.home() / ".claude" / ".claude-code-memory" / "discussion.db"
TITLE_MAX = 20
_COLON_CHARS = (":", "：")

CODE_TO_TABLE: dict[str, tuple[str, bool]] = {
    "M": ("materials", True),
    "D": ("decisions", True),
    "L": ("discussion_logs", True),
    "A": ("activities", False),
    "T": ("discussion_topics", False),
}


def _db_path() -> str:
    return os.environ.get("CC_MEMORY_DB_PATH", str(DEFAULT_DB_PATH))


def _strip_prefix(title: str) -> str:
    """タイトルに最初の `:` / `：` が含まれていたらそれより前を捨てる。

    例: `[第三者吟味] peer-b: 提案` → `提案`。プレフィックスはユーザー画面で
    幅を取るが情報量が薄いため落として実本文だけ見せる。コロン直後が空文字
    (lstrip 後に何も残らない) になる場合は元タイトルを返す。
    """
    best = -1
    for sep in _COLON_CHARS:
        idx = title.find(sep)
        if idx >= 0 and (best < 0 or idx < best):
            best = idx
    if best < 0:
        return title
    tail = title[best + 1 :].lstrip()
    return tail or title


def _truncate(title: str) -> str:
    if len(title) > TITLE_MAX:
        return title[:TITLE_MAX] + "…"
    return title


def _fetch_display(conn: sqlite3.Connection, code: str, id_int: int) -> str | None:
    """`(...)` の中身に入れる文字列を返す。不在 / 空タイトル時は None。"""
    entry = CODE_TO_TABLE.get(code)
    if entry is None:
        return None
    table, has_retracted = entry
    if has_retracted:
        sql = f"SELECT title, retracted_at FROM {table} WHERE id = ?"
    else:
        sql = f"SELECT title, NULL FROM {table} WHERE id = ?"
    row = conn.execute(sql, (id_int,)).fetchone()
    if row is None:
        return None
    title, retracted_at = row[0], row[1]
    if not title:
        return None
    display = _truncate(_strip_prefix(title))
    if retracted_at is not None:
        return f"{display}, 取消済"
    return display


def _wrap(
    match: "object",
    code: str,
    id_int: int,
    cache: dict[tuple[str, int], str | None],
    conn: sqlite3.Connection,
) -> str:
    original = match.group(0)
    end = match.end()
    text = match.string
    # idempotent: 直後に ` (` が続いていたら何もしない
    if end < len(text) - 1 and text[end] == " " and text[end + 1] == "(":
        return original
    key = (code, id_int)
    if key not in cache:
        cache[key] = _fetch_display(conn, code, id_int)
    title = cache[key]
    if title is None:
        return original
    return f"{original} ({title})"


def _enrich(text: str, conn: sqlite3.Connection) -> str:
    cache: dict[tuple[str, int], str | None] = {}

    def replace_code(match):
        code = match.group(1)
        id_int = int(match.group(2))
        return _wrap(match, code, id_int, cache, conn)

    def replace_fullword(match):
        word = match.group(1).lower()
        code = FULLWORD_TO_CODE[word]
        id_int = int(match.group(2))
        return _wrap(match, code, id_int, cache, conn)

    enriched = RAW_CITE_CODE_PATTERN.sub(replace_code, text)
    enriched = RAW_CITE_FULLWORD_PATTERN.sub(replace_fullword, enriched)
    return enriched


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    message = payload.get("delta") or payload.get("assistant_message")
    if not isinstance(message, str) or not message:
        sys.exit(0)

    db_path = _db_path()
    if not pathlib.Path(db_path).exists():
        sys.exit(0)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        sys.exit(0)

    try:
        enriched = _enrich(message, conn)
    except Exception:
        sys.exit(0)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    if enriched == message:
        sys.exit(0)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "MessageDisplay",
            "displayContent": enriched,
        }
    }
    json.dump(output, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
