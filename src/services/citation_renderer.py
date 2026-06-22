"""citation テンプレ (`{{cite:X#NNN}}`) を flavor に応じて展開する。

3 flavor:
- raw      : 本文無加工 (テンプレも deleted/retracted 加工もしない)
- internal : `<title> (X#NNN)` 形式 (AI エージェント向け、ID 保持)
- readable : `<title>` 形式 (人間向け、ID 出力なし)

target が物理削除 / retracted (decision/log) の場合:
- internal : `[deleted X#NNN]` / `[retracted X#NNN]`
- readable : `[deleted item]` / `[retracted item]`

コードブロック内 / `\\{{cite:...}}` エスケープはどの flavor でも無加工で残す。
"""
import re
import sqlite3
from typing import Literal

from src.services.citations_pure import (
    TYPE_CODE_TO_NAME,
    TYPE_NAME_TO_CODE,
    _CITE_PATTERN,
)
from src.services.citations_service import (
    _get_in_out_with_conn,
    _resolve_targets,
)

Flavor = Literal["raw", "internal", "readable"]
VALID_FLAVORS = ("raw", "internal", "readable")
DEFAULT_FLAVOR: Flavor = "internal"

# read response 内で展開を試みるフィールド名 (DB カラム名と一致するキーのみ対象)
RESPONSE_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "material": ("title", "content"),
    "decision": ("title", "decision", "reason"),
    "log": ("title", "content"),
    "activity": ("title", "description"),
    "topic": ("title", "description"),
}


def expand(content: str, flavor: Flavor, conn: sqlite3.Connection) -> str:
    """本文中の `{{cite:X#NNN}}` テンプレを flavor に応じて展開する。

    None 入力時は空文字を返す (戻り値型 `str` 契約を維持するため)。
    """
    if content is None:
        return ""
    if flavor == "raw":
        return content
    if flavor not in VALID_FLAVORS:
        raise ValueError(f"Invalid flavor {flavor!r}; must be one of {VALID_FLAVORS}")

    spans = _collect_spans(content)
    if not spans:
        return content
    pairs = list({(target_type, target_id) for _, _, target_type, target_id in spans})
    resolved = _resolve_targets(conn, pairs)
    # 逆順で置換 (オフセットがズレないように)
    parts: list[str] = []
    cursor = 0
    for start, end, target_type, target_id in spans:
        parts.append(content[cursor:start])
        meta = resolved.get((target_type, target_id))
        parts.append(_render_one(flavor, target_type, target_id, meta))
        cursor = end
    parts.append(content[cursor:])
    return "".join(parts)


def _render_one(flavor: Flavor, target_type: str, target_id: int, meta: dict | None) -> str:
    code = TYPE_NAME_TO_CODE[target_type]
    full_id = f"{code}#{target_id}"
    deleted = bool(meta and meta.get("deleted"))
    retracted = bool(meta and meta.get("retracted"))
    if deleted:
        if flavor == "internal":
            return f"[deleted {full_id}]"
        return "[deleted item]"
    if retracted:
        if flavor == "internal":
            return f"[retracted {full_id}]"
        return "[retracted item]"
    title = (meta or {}).get("title") or full_id
    if flavor == "internal":
        return f"{title} ({full_id})"
    return title


def _collect_spans(content: str) -> list[tuple[int, int, str, int]]:
    """本文中の有効な citation テンプレ位置を出現順に集める。

    コードブロック / エスケープ / 不正形式はスキップ。

    Returns:
        [(start, end, target_type, target_id), ...]
    """
    spans: list[tuple[int, int, str, int]] = []
    lines = content.split("\n")
    line_offset = 0  # content[line_offset:line_offset+len(line)] が現在行
    in_fence = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            line_offset += len(line) + 1
            continue
        if in_fence:
            line_offset += len(line) + 1
            continue
        spans.extend(_scan_line_for_spans(line, line_offset))
        line_offset += len(line) + 1
    return spans


def _scan_line_for_spans(line: str, base: int) -> list[tuple[int, int, str, int]]:
    out: list[tuple[int, int, str, int]] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "`":
            close = line.find("`", i + 1)
            if close == -1:
                break
            i = close + 1
            continue
        if ch == "\\" and line[i + 1 : i + 3] == "{{":
            end = line.find("}}", i + 1)
            if end == -1:
                i += 1
                continue
            i = end + 2
            continue
        m = _CITE_PATTERN.match(line, i)
        if m:
            code = m.group(1)
            target_type = TYPE_CODE_TO_NAME.get(code)
            if target_type:
                try:
                    target_id = int(m.group(2))
                    out.append((base + m.start(), base + m.end(), target_type, target_id))
                except ValueError:
                    pass
            i = m.end()
            continue
        i += 1
    return out


def apply_flavor_to_entity_dict(
    d: dict,
    entity_type: str,
    flavor: Flavor,
    conn: sqlite3.Connection,
    id_key: str = "id",
    attach_citations: bool = True,
) -> dict:
    """read response の単一エンティティ dict に対し flavor 展開 + citations_in/out を貼る。

    Args:
        d: 改変対象 dict (in-place 改変)
        entity_type: "material" / "decision" / "log" / "activity" / "topic"
        flavor: 展開フォーマット
        conn: DB 接続
        id_key: dict 内の ID キー名 (デフォルト "id"。material_id 等の場合は上書き)
        attach_citations: True のとき citations_in/citations_out を追加

    Returns:
        改変済み dict (同オブジェクト)
    """
    if not isinstance(d, dict) or entity_type not in RESPONSE_TEXT_FIELDS:
        return d
    if flavor != "raw":
        for field in RESPONSE_TEXT_FIELDS[entity_type]:
            if field in d and isinstance(d[field], str) and d[field]:
                d[field] = expand(d[field], flavor, conn)
    if attach_citations:
        entity_id = _extract_entity_id(d, entity_type, id_key)
        if entity_id is not None:
            io = _get_in_out_with_conn(conn, entity_type, entity_id)
            d["citations_out"] = io["out"]
            d["citations_in"] = io["in"]
    return d


def _extract_entity_id(d: dict, entity_type: str, id_key: str) -> int | None:
    """response dict から整数 ID を取り出す。

    apply_readable_id_inplace で `id` / `<entity>_id` が文字列化されている場合に
    備えて `<key>_raw` フィールドにフォールバックする。"""
    for key in (
        id_key,
        f"{entity_type}_id",
        f"{id_key}_raw",
        f"{entity_type}_id_raw",
        "id_raw",
    ):
        v = d.get(key)
        if isinstance(v, int):
            return v
    return None


def apply_flavor_to_snippet(
    snippet: str, flavor: Flavor, conn: sqlite3.Connection
) -> str:
    """snippet 文字列に raw 境界調整 → flavor 展開を適用する。

    None 入力時は空文字を返す (戻り値型 `str` 契約を維持するため)。
    """
    if snippet is None:
        return ""
    if flavor == "raw":
        return snippet
    adjusted = adjust_snippet_boundary(snippet)
    return expand(adjusted, flavor, conn)


def adjust_snippet_boundary(snippet: str) -> str:
    """snippet 文字列が境界で `{{cite:...}}` テンプレを半端に切ったとき、
    テンプレ全体を含めるか除外するかで境界を調整する。

    snippet は raw 段階で受け取り、調整後に flavor 展開する想定。

    調整方針:
    - 先頭: `}}` で閉じる前にテンプレ開始が来ていれば、その先頭まで切り詰める
    - 末尾: `{{cite:` の途中で切れていれば、その手前で切る
    """
    if not snippet:
        return snippet
    adjusted = snippet
    # 末尾側: 末尾近くで `{{cite:...` が始まったが `}}` で閉じていないケース
    last_open = adjusted.rfind("{{cite:")
    if last_open != -1:
        close_after = adjusted.find("}}", last_open)
        if close_after == -1:
            adjusted = adjusted[:last_open]
    # 先頭側: 先頭近くで `...}}` で閉じているが対応する `{{cite:` が無いケース
    first_close = adjusted.find("}}")
    if first_close != -1:
        open_before = adjusted.rfind("{{cite:", 0, first_close)
        if open_before == -1:
            adjusted = adjusted[first_close + 2 :]
    return adjusted
