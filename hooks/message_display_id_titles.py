"""MessageDisplay hook: assistant 発話 chunk の内部 ID リテラル
(`[MDLAT]#NNN` または英語フルワード `log/decision/activity/material/topic #NNN`)
の直後にエンティティタイトルを差し込んで表示する。

`hookSpecificOutput.displayContent` で表示のみ書き換える。transcript と
Claude context には元のテキストが残るため、AI 側の token 消費は変わらない。
ユーザー画面でだけ可読性が上がる。応答の出力はHarness経由で、表示書き換え
機構が無いハーネス (Codex。MessageDisplay相当イベントが存在せず
emit_display_content が False) では何も出力せず終了する。表示整形のみの
機能のため、Codex側の代替実装は行わない (方針の経緯は #617 を参照)。

MessageDisplay は実機では `delta` + `index` + `final` + `message_id` の
streaming protocol で発火する。本 hook は chunk 単位 (`delta`) で補完を行い、
chunk 境界をまたぐ ID リテラル (例: chunk N が `M#`、chunk N+1 が `123 と`)
は補完漏れになる trade-off を受け入れる。`final=true` 待ち全文累積モデルは
今回採用しない。

同じ仕組みで、稼働中セッションの CLI が付けた表示名 (`workspace-1b` の
ような形) も `<Session: 表示名>` に丸ごと置き換える。対応表
(`session_aliases.json`、`registry_path()`) に実在する name だけを対象にし、
前後が英数字・ハイフン・アンダースコアに続く場合は照合しない (`workspace-1bc`
の中の `workspace-1b` を拾わない)。バッククォート 1 個で挟まれたインライン
コード内の一致は対象外にするが、判定は chunk 単体で行い chunk をまたぐ
コードブロックは検出しない。対応表が無い・壊れている場合はセッション名の
置換だけを諦め、内部 ID の併記は動き続ける。

calm project 内かどうかは判定せず、全 session で有効。
"""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import sys

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from src.env_compat import env_get  # noqa: E402
from src.harness import select_harness  # noqa: E402
from src.services.internal_id_patterns import FULLWORD_TO_CODE  # noqa: E402

# code 形式と fullword 形式を 1 つの regex にまとめる。2 段階 sub にすると
# 第 1 パスで挿入したタイトル中に含まれる fullword リテラル (タイトルに
# fullword 形式の参照が混入しているケース) を第 2 パスが再 enrich してネスト
# 括弧になるため、単一 regex の単一パスで処理する (re.sub は置換結果を scan
# しないので、title 内に偶然マッチする ID が含まれても再 enrich されない)。
# code 部分は大文字限定、fullword 部分のみ inline flag (?i:...) で
# case-insensitive にする。セッション名を 1 件以上照合するときは、この
# フラグメントに `sess` alternative を追加した regex を都度組み立てる
# (`_build_pattern`)。セッション名は対応表を読むまで内容が分からないため
# module import 時点では組み立てられない。
_ID_FRAGMENT = (
    r"(?<![A-Za-z0-9_/])"
    r"(?:(?P<code>[MDLAT])#|(?i:(?P<fullword>log|decision|activity|material|topic) ?#))"
    r"(?P<num>\d+)"
    r"(?![A-Za-z0-9_])"
)
_COMBINED_PATTERN = re.compile(_ID_FRAGMENT)

DEFAULT_DB_PATH = pathlib.Path.home() / ".claude" / ".claude-code-memory" / "discussion.db"
TITLE_MAX = 40
_COLON_CHARS = (":", "：")
_BRACKET_TAG_RE = re.compile(r"^\[[^\]]*\]\s*")
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")

CODE_TO_TABLE: dict[str, tuple[str, bool]] = {
    "M": ("materials", True),
    "D": ("decisions", True),
    "L": ("discussion_logs", True),
    "A": ("activities", False),
    "T": ("discussion_topics", False),
}


def _db_path() -> str:
    return env_get("CALM_DB_PATH", str(DEFAULT_DB_PATH))


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


def _strip_bracket_tag(title: str) -> str:
    """先頭の `[作業] ` のような角カッコの札を1つだけ外す。"""
    return _BRACKET_TAG_RE.sub("", title, count=1)


def _session_display(entry: dict) -> str:
    """セッション名の置換先文字列を返す。空文字は「置換しない」を意味する。"""
    if entry.get("alias_source") == "manual":
        raw = entry.get("alias")
        text = raw if isinstance(raw, str) else ""
    else:
        raw = entry.get("activity_title")
        text = _strip_bracket_tag(raw) if isinstance(raw, str) else ""
    text = text.strip()
    return _truncate(text) if text else ""


def _session_name_displays() -> dict[str, str]:
    """対応表 (`get_sessions` と同じ JSON) から `name → 表示名` を返す。

    読み取り専用でロックは取らない。書き込み側 (register_checkin/set_alias)
    は tmp file → os.replace の atomic rename で更新するため、ロック無しでも
    torn read は起きない。本 hook は delta chunk ごとに高頻度で呼ばれるため、
    書き込み側のロック待ちに巻き込まれる latency を避ける。import 自体を
    try に含めるのは、依存モジュール側の予期しない例外で ID 併記まで
    巻き込んで止めないため。ファイル不在・壊れた JSON・想定外の型・
    import 失敗はすべて空 dict (fail open: セッション名の置換だけを諦める)。
    """
    try:
        from src.services.session_registry_service import registry_path

        path = registry_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            return {}
    except Exception:
        return {}

    displays: dict[str, str] = {}
    for entry in sessions.values():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        display = _session_display(entry)
        if display:
            displays[name] = display
    return displays


def _build_pattern(session_names: list[str]) -> re.Pattern[str]:
    """ID 用 regex に、対応表にある session name の alternative を追加する。

    session_names が空なら ID だけの `_COMBINED_PATTERN` をそのまま返す
    (今までどおりの挙動)。session name 側の前後境界は英数字・ハイフン・
    アンダースコアで、ID 側 (`_ID_FRAGMENT`) とは別の文字クラスを使う。
    """
    if not session_names:
        return _COMBINED_PATTERN
    ordered = sorted(session_names, key=len, reverse=True)
    sess_alt = "|".join(re.escape(name) for name in ordered)
    return re.compile(
        _ID_FRAGMENT
        + rf"|(?<![A-Za-z0-9_-])(?P<sess>{sess_alt})(?![A-Za-z0-9_-])"
    )


def _within_code_span(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


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
    session_displays = _session_name_displays()
    pattern = _build_pattern(list(session_displays.keys()))
    code_spans = [m.span() for m in _CODE_SPAN_RE.finditer(text)] if session_displays else []

    def replace(match):
        sess = match.groupdict().get("sess")
        if sess is not None:
            if _within_code_span(match.start(), match.end(), code_spans):
                return match.group(0)
            return f"<Session: {session_displays[sess]}>"
        code_letter = match.group("code")
        fullword = match.group("fullword")
        id_int = int(match.group("num"))
        code = code_letter if code_letter is not None else FULLWORD_TO_CODE[fullword.lower()]
        return _wrap(match, code, id_int, cache, conn)

    return pattern.sub(replace, text)


def main() -> None:
    harness = select_harness(hook_event_name="MessageDisplay")
    try:
        payload = harness.read_hook_input()
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

    harness.emit_display_content(enriched)


if __name__ == "__main__":
    main()
