"""citation 参照テンプレ (`{{cite:X#NNN}}`) を扱う pure な層。

- 全 entity 種別の名前/コード/テーブル/タイトル取得式の対応表
- 本文中の `{{cite:X#NNN}}` 抽出 (コードブロック・エスケープ尊重)
- 生 `X#NNN` リテラルから `{{cite:X#NNN}}` への変換 (transcript sanitize 用)
- target 存在チェック (read-only conn 注入)

`from src.db import get_connection` は呼ばない。DB へのアクセスは引数 conn 経由のみ。
`src.services.citations_service` (write 経路、DB 接続を内部で開く高レベル API) と、
transcript sanitize hook の両方からこの層を import する。
"""
import logging
import re
import sqlite3
from typing import Callable

logger = logging.getLogger(__name__)

# 同パッケージの他モジュール (citations_service / citation_renderer) と、
# transcript sanitize hook が import する公開シンボルを明示する。アンダースコア
# プレフィックスのままだが「pure 層が抱える公開 API」として明示的に export する。
__all__ = [
    "VALID_OWNER_TYPES",
    "VALID_TARGET_TYPES",
    "TYPE_CODE_TO_NAME",
    "TYPE_NAME_TO_CODE",
    "TYPE_TO_TABLE",
    "TYPE_TO_TITLE_EXPR",
    "TYPES_WITH_RETRACT",
    "OWNER_TEXT_FIELDS",
    "_CITE_PATTERN",
    "_CITE_LIKE_PATTERN",
    "_RAW_CITE_PATTERN",
    "extract_citations",
    "convert_raw_to_cite",
    "check_target_exists",
    "_validate_owner_type",
    "_combine_owner_text",
]

VALID_OWNER_TYPES = ("material", "decision", "log", "activity", "topic")
VALID_TARGET_TYPES = VALID_OWNER_TYPES

TYPE_CODE_TO_NAME: dict[str, str] = {
    "M": "material",
    "D": "decision",
    "L": "log",
    "A": "activity",
    "T": "topic",
}
TYPE_NAME_TO_CODE: dict[str, str] = {v: k for k, v in TYPE_CODE_TO_NAME.items()}

TYPE_TO_TABLE: dict[str, str] = {
    "material": "materials",
    "decision": "decisions",
    "log": "discussion_logs",
    "activity": "activities",
    "topic": "discussion_topics",
}

# 各 entity 種別の表示タイトル取得式 (SELECT 内で使用)。
# decision は title が NULL のとき decision 本文へ fall back する。
TYPE_TO_TITLE_EXPR: dict[str, str] = {
    "material": "title",
    "decision": "COALESCE(NULLIF(TRIM(title), ''), substr(decision, 1, 80))",
    "log": "COALESCE(NULLIF(TRIM(title), ''), substr(content, 1, 30))",
    "activity": "title",
    "topic": "title",
}

# retract カラムを持つ entity 種別
TYPES_WITH_RETRACT = {"decision", "log", "material"}

# owner 種別ごとに、本文中の citation 抽出対象となるテキストフィールド
# (DB カラム名そのまま、結合順は occurrence の決定要因)
OWNER_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "material": ("title", "content"),
    "decision": ("decision", "reason"),
    "log": ("content",),
    "activity": ("title", "description"),
    "topic": ("title", "description"),
}

_CITE_PATTERN = re.compile(r"\{\{cite:([MDLAT])#(\d+)\}\}")
_CITE_LIKE_PATTERN = re.compile(r"\{\{cite:[^}]*\}\}")

# 生 `X#NNN` 検出パターン。word boundary を lookbehind/lookahead で明示する
# (前後が英数字/_/ なら識別子の一部とみなして非マッチ)。
_RAW_CITE_PATTERN = re.compile(r"(?<![A-Za-z0-9_/])([MDLAT])#(\d+)(?![A-Za-z0-9_])")


def extract_citations(content: str) -> list[tuple[str, int]]:
    """本文から citation 参照を出現順に抽出する。

    コードブロック (フェンス ``` / ~~~ と インラインバッククォート) 内の
    テンプレはスキップする。`\\{{cite:...}}` のエスケープもスキップする。
    不正形式 (`{{cite:Z#1}}`, `{{cite:foo}}` 等) は警告ログを出して無視する。

    Returns:
        [(target_type, target_id), ...] の出現順リスト。occurrence は 1 始まりで連番。
    """
    results: list[tuple[str, int]] = []
    in_fence = False
    for raw_line in content.split("\n"):
        stripped = raw_line.lstrip()
        # フェンス境界 (```/~~~ で始まる行) でトグル
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        results.extend(_scan_line(raw_line))
    return results


def _scan_line(line: str) -> list[tuple[str, int]]:
    """1 行内の citation を走査。インラインバッククォート / エスケープをスキップ。"""
    out: list[tuple[str, int]] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "`":
            # 対応する閉じバッククォートまでスキップ
            close = line.find("`", i + 1)
            if close == -1:
                # 未閉じインラインコード: 行末まで保守的にスキップ
                break
            i = close + 1
            continue
        if ch == "\\" and line[i + 1 : i + 3] == "{{":
            # エスケープ `\{{cite:...}}` 全体をスキップ
            end = line.find("}}", i + 1)
            if end == -1:
                i += 1
                continue
            i = end + 2
            continue
        m = _CITE_PATTERN.match(line, i)
        if m:
            code = m.group(1)
            target_id_str = m.group(2)
            target_type = TYPE_CODE_TO_NAME.get(code)
            if target_type is None:
                logger.warning("citation parser: unknown type code %r", code)
                i = m.end()
                continue
            try:
                target_id = int(target_id_str)
            except ValueError:
                logger.warning("citation parser: invalid id %r", target_id_str)
                i = m.end()
                continue
            out.append((target_type, target_id))
            i = m.end()
            continue
        # 不正形式テンプレ (`{{cite:foo}}` 等) は警告
        like = _CITE_LIKE_PATTERN.match(line, i)
        if like:
            logger.warning("citation parser: malformed template skipped: %r", like.group(0))
            i = like.end()
            continue
        i += 1
    return out


def _validate_owner_type(owner_type: str) -> None:
    if owner_type not in VALID_OWNER_TYPES:
        raise ValueError(
            f"Invalid owner_type {owner_type!r}; must be one of {VALID_OWNER_TYPES}"
        )


def _combine_owner_text(owner_type: str, fields: dict) -> str:
    """owner の本文を occurrence 計算用に決定的順序で結合する。"""
    cols = OWNER_TEXT_FIELDS[owner_type]
    return "\n".join(fields.get(c) or "" for c in cols)


def _new_convert_counters() -> dict:
    return {
        "sanitized_count": 0,
        "skipped_in_codeblock": 0,
        "skipped_in_existing_cite": 0,
        "skipped_escape": 0,
        "skipped_dangling": 0,
    }


def _count_raw_in_segment(segment: str) -> int:
    """セグメント内の生 `X#NNN` パターン数を boundary 付きで数える。"""
    return len(_RAW_CITE_PATTERN.findall(segment))


def _convert_line_raw_to_cite(
    line: str,
    target_validator: Callable[[str, int], bool] | None,
    counters: dict,
) -> str:
    """1 行内の生 `X#NNN` を `{{cite:X#NNN}}` に変換する。

    スキップ対象:
    - インラインバッククォート区間 (`...`)
    - エスケープ `\\X#NNN` (バッククスラッシュ直後の生リテラル)
    - 既に `{{cite:X#NNN}}` の中にあるもの (二重変換防止)
    各スキップ区間に含まれる生リテラル数は counters の対応キーへ加算する。
    target_validator が False を返した target は変換せず skipped_dangling にカウント。
    """
    out_parts: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        # インラインバッククォート: 対応する閉じまで丸ごとスキップ
        if ch == "`":
            close = line.find("`", i + 1)
            if close == -1:
                # 未閉じ: 行末まで保守的にスキップ
                segment = line[i:]
                counters["skipped_in_codeblock"] += _count_raw_in_segment(segment)
                out_parts.append(segment)
                i = n
                continue
            segment = line[i : close + 1]
            counters["skipped_in_codeblock"] += _count_raw_in_segment(segment)
            out_parts.append(segment)
            i = close + 1
            continue
        # 既存 `{{cite:...}}` テンプレ: 内部の `X#NNN` は変換しない
        if line[i : i + 7] == "{{cite:":
            end = line.find("}}", i + 7)
            if end == -1:
                out_parts.append(ch)
                i += 1
                continue
            segment = line[i : end + 2]
            counters["skipped_in_existing_cite"] += _count_raw_in_segment(segment)
            out_parts.append(segment)
            i = end + 2
            continue
        # エスケープ: `\X#NNN` または `\{{cite:...}}`
        if ch == "\\":
            # `\{{cite:...}}` は既存パーサと同じく全体スキップ (エスケープ扱い)
            if line[i + 1 : i + 3] == "{{":
                end = line.find("}}", i + 1)
                if end == -1:
                    out_parts.append(ch)
                    i += 1
                    continue
                segment = line[i : end + 2]
                counters["skipped_escape"] += _count_raw_in_segment(segment)
                out_parts.append(segment)
                i = end + 2
                continue
            # `\X#NNN`: バックスラッシュは lookbehind 非該当なので明示判定する
            tail = line[i + 1 : i + 2]
            if tail in TYPE_CODE_TO_NAME:
                m = _RAW_CITE_PATTERN.match(line, i + 1)
                if m and m.start() == i + 1:
                    out_parts.append(line[i : m.end()])
                    counters["skipped_escape"] += 1
                    i = m.end()
                    continue
            out_parts.append(ch)
            i += 1
            continue
        # 生 `X#NNN`
        m = _RAW_CITE_PATTERN.match(line, i)
        if m:
            code = m.group(1)
            target_id = int(m.group(2))
            target_type = TYPE_CODE_TO_NAME[code]
            if target_validator is not None and not target_validator(target_type, target_id):
                out_parts.append(line[i : m.end()])
                counters["skipped_dangling"] += 1
                i = m.end()
                continue
            out_parts.append("{{cite:" + code + "#" + str(target_id) + "}}")
            counters["sanitized_count"] += 1
            i = m.end()
            continue
        out_parts.append(ch)
        i += 1
    return "".join(out_parts)


def convert_raw_to_cite(
    text: str,
    *,
    target_validator: Callable[[str, int], bool] | None = None,
) -> tuple[str, dict]:
    """テキスト中の生 `X#NNN` を `{{cite:X#NNN}}` に変換する (sanitize)。

    フェンスコードブロック (``` / ~~~)、インラインバッククォート、エスケープ
    (`\\X#NNN` / `\\{{cite:...}}`)、既に `{{cite:X#NNN}}` 形式のものはスキップする。
    冪等: `convert_raw_to_cite(convert_raw_to_cite(text)[0])[0] == convert_raw_to_cite(text)[0]`。

    Args:
        text: 入力テキスト
        target_validator: `(target_type, target_id) -> bool` を返す関数。
            None なら存在チェックなし (debug モード、全変換)。
            False を返した target は変換せず skipped_dangling にカウントする。

    Returns:
        (変換後テキスト, counters)
        counters = {
            "sanitized_count": 変換した生リテラル数,
            "skipped_in_codeblock": フェンス + インラインバッククォート内に含まれた生リテラル数,
            "skipped_in_existing_cite": 既存 `{{cite:...}}` 内に含まれた生リテラル数,
            "skipped_escape": `\\X#NNN` エスケープでスキップした生リテラル数,
            "skipped_dangling": target_validator が False を返してスキップした target 数,
        }
    """
    counters = _new_convert_counters()
    out_lines: list[str] = []
    in_fence = False
    lines = text.split("\n")
    for raw_line in lines:
        stripped = raw_line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out_lines.append(raw_line)
            continue
        if in_fence:
            counters["skipped_in_codeblock"] += _count_raw_in_segment(raw_line)
            out_lines.append(raw_line)
            continue
        out_lines.append(_convert_line_raw_to_cite(raw_line, target_validator, counters))
    return "\n".join(out_lines), counters


def check_target_exists(
    conn: sqlite3.Connection, target_type: str, target_id: int
) -> bool:
    """target が DB に物理的に存在するか判定する (retracted_at は無視)。

    引数 conn は呼び出し元が開いた接続を使う (read-only でもよい)。
    target_type が未知のときは False を返す。
    """
    if target_type not in TYPE_TO_TABLE:
        return False
    table = TYPE_TO_TABLE[target_type]
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1", (target_id,)
    ).fetchone()
    return row is not None
