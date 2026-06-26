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
import re
import sqlite3
import sys

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from hooks.hook_state import HookState  # noqa: E402
from src.services.internal_id_patterns import FULLWORD_TO_CODE  # noqa: E402

# code 形式と fullword 形式を 1 つの regex にまとめる。2 段階 sub にすると
# 第 1 パスで挿入したタイトル中に含まれる fullword リテラル (タイトルに
# fullword 形式の参照が混入しているケース) を第 2 パスが再 enrich してネスト
# 括弧になるため、単一 regex の単一パスで処理する (re.sub は置換結果を scan
# しないので、title 内に偶然マッチする ID が含まれても再 enrich されない)。
# code 部分は大文字限定、fullword 部分のみ inline flag (?i:...) で
# case-insensitive にする。
_COMBINED_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_/])"
    r"(?:([MDLAT])#|(?i:(log|decision|activity|material|topic) ?#))"
    r"(\d+)"
    r"(?![A-Za-z0-9_])"
)

DEFAULT_DB_PATH = pathlib.Path.home() / ".claude" / ".claude-code-memory" / "discussion.db"
TITLE_MAX = 40
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

    def replace(match):
        code_letter = match.group(1)
        fullword = match.group(2)
        id_int = int(match.group(3))
        code = code_letter if code_letter is not None else FULLWORD_TO_CODE[fullword.lower()]
        return _wrap(match, code, id_int, cache, conn)

    return _COMBINED_PATTERN.sub(replace, text)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    # 環境変数による HookState BASE_DIR オーバーライド (テスト用)
    if os.environ.get("HOOK_STATE_DIR"):
        HookState.BASE_DIR = pathlib.Path(os.environ["HOOK_STATE_DIR"])

    message = payload.get("delta") or payload.get("assistant_message")
    if not isinstance(message, str) or not message:
        sys.exit(0)

    # 観測副作用: assistant 発話に含まれる cc-memory 内部 ID リテラル件数を
    # session 単位 state に蓄積する。UserPromptSubmit hook が次ターンに
    # この値を参照して system-reminder を注入する。DB アクセスより前に
    # 実行するため DB 不在でも観測は有効。
    session_id = payload.get("session_id", "")
    if session_id:
        try:
            matches = _COMBINED_PATTERN.findall(message)
            if matches:
                HookState(session_id).increment_id_leak_count(len(matches))
        except Exception:
            # 観測失敗で本体の補完表示動作を止めない
            pass

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
