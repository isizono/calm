"""decision / material の reason・content 本文に書く定型節（判例規約）を扱う pure な層。

reason 本文の末尾に、行頭見出しで始まる節を置く運用規約をパースする。
見出しは 5 種類: `却下案:` / `適用条件:` / `適用外:` / `検証:` / `隣接確認:`。
書式の正本は `docs/precedent-format.md`。

本モジュールが定型節・検証アンカーの唯一のパーサ実装である。消費者（decision の
読み出し面付与、判例クラスタ展開、鮮度メタデータ、検索の権威 boost 等）は全て
本モジュールを import する。各消費側コンポーネントが独自の文法・正規表現を持つ
ことを禁じ、文法変更は本モジュール + `docs/precedent-format.md` の 1 箇所で行う。

DB アクセス・ファイル I/O は持たない（`citations_pure.py` の分離慣行に合わせる）。
"""
import re

__all__ = [
    "SECTION_HEADERS",
    "NEAR_MISS_HEADERS",
    "parse_precedent_sections",
    "summarize_precedent",
    "attach_precedent",
]

# 正規の節見出し語彙（行頭に置き、`:` で終える）
SECTION_HEADERS = ("却下案", "適用条件", "適用外", "検証", "隣接確認")

# 表記ゆれ検出用（節として採らず warning を出す近似見出し）
NEAR_MISS_HEADERS = (
    "却下例", "棄却案", "不採用案", "適用範囲", "対象外", "検証済み", "rejected", "scope",
    "近接確認", "隣接チェック", "周辺確認",
)

# 却下案 / 適用条件 / 適用外 / 隣接確認: 見出し行のみで完結し、以降の箇条書き行が項目になる。
# コロンは半角・全角どちらも許容する（却下案項目の区切りが全角を許すのと揃える）。
_LIST_HEADING_RE = re.compile(r"^(却下案|適用条件|適用外|隣接確認)\s*[:：]\s*$")
# 検証: 見出しと内容が同一行（複数行許容、行ごとに独立したアンカーとして扱う）
_VERIFY_HEADING_RE = re.compile(r"^検証\s*[:：]\s*(.*)$")
# 節本文の箇条書き項目
_ITEM_RE = re.compile(r"^-\s+(.*)$")

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# 7〜40 桁の hex 文字列（コミット SHA）。word boundary で区切って抽出する。
_SHA_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")

_SECTION_KEY = {
    "却下案": "rejected_alternatives",
    "適用条件": "scope_in",
    "適用外": "scope_out",
    "隣接確認": "adjacent_check",
}

_NEAR_MISS_PATTERNS = tuple(
    (header, re.compile(rf"^{re.escape(header)}\s*[:：]", re.IGNORECASE))
    for header in NEAR_MISS_HEADERS
)


def _split_rejected_item(text: str) -> tuple[str, str, bool]:
    """却下案の箇条書き項目を `案: 理由` の 2 分割にする。

    区切りが無ければ alternative のみとし、reason は空文字にする。
    Returns: (alternative, reason, has_separator)
    """
    idx = text.find(": ")
    if idx != -1:
        return text[:idx].strip(), text[idx + 2:].strip(), True
    idx = text.find("：")
    if idx != -1:
        return text[:idx].strip(), text[idx + 1:].strip(), True
    return text.strip(), "", False


def parse_precedent_sections(reason: str) -> dict | None:
    """reason（または material の content）本文から定型節をパースする。

    行単位の状態機械で処理する。`^(却下案|適用条件|適用外|隣接確認)\\s*[:：]\\s*$`
    （見出し行のみ）で節を開き、次の見出しか本文終端で閉じる。`^検証\\s*[:：]` は同一行に
    内容を取り、行ごとに独立したアンカーとして扱う（複数行許容）。見出しのコロンは半角・
    全角どちらも許容する。見出し行より前・節の外にあるテキストは自由記述本文として
    そのまま無視する（本文を書き換えない）。

    Args:
        reason: パース対象の本文（decision の reason、または material の content）。

    Returns:
        正規の節見出し・近似見出しのいずれも 1 つも検出されなければ None を返す
        （定型節規約が適用されていない legacy 本文との判別に使う）。
        1 つでも検出されれば:
        {
          "rejected_alternatives": [{"alternative": str, "reason": str}, ...],
          "scope_in":  [str, ...],
          "scope_out": [str, ...],
          "verification_anchors": [
            {"raw": str, "date": str | None, "commit": str | None}, ...
          ],
          "adjacent_check": [{"axis": str, "note": str}, ...],
          "warnings": [str, ...],   # 表記ゆれ・空節・アンカー日付欠落など
        }
    """
    if not reason:
        return None

    lines = reason.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    rejected_alternatives: list[dict] = []
    scope_in: list[str] = []
    scope_out: list[str] = []
    verification_anchors: list[dict] = []
    adjacent_check: list[dict] = []
    warnings: list[str] = []

    any_marker = False
    current_section: str | None = None
    current_items: list = []

    def _close_section() -> None:
        nonlocal current_section, current_items
        if current_section is None:
            return
        if not current_items:
            warnings.append(f"empty section: {current_section}:")
        else:
            key = _SECTION_KEY[current_section]
            if key == "rejected_alternatives":
                rejected_alternatives.extend(current_items)
            elif key == "scope_in":
                scope_in.extend(current_items)
            elif key == "adjacent_check":
                adjacent_check.extend(current_items)
            else:
                scope_out.extend(current_items)
        current_section = None
        current_items = []

    for raw_line in lines:
        m_list = _LIST_HEADING_RE.match(raw_line)
        if m_list:
            _close_section()
            current_section = m_list.group(1)
            current_items = []
            any_marker = True
            continue

        m_verify = _VERIFY_HEADING_RE.match(raw_line)
        if m_verify:
            _close_section()
            any_marker = True
            raw_content = m_verify.group(1).strip()
            if not raw_content:
                # 内容の無い `検証:` 行はアンカーとして採らない（空文字 raw が
                # verification_anchors に混ざると「空リスト=決定のみ」判別が崩れる）。
                warnings.append("empty verification heading: 検証:")
                continue
            date_m = _DATE_RE.search(raw_content)
            sha_m = _SHA_RE.search(raw_content)
            anchor = {
                "raw": raw_content,
                "date": date_m.group(0) if date_m else None,
                "commit": sha_m.group(0) if sha_m else None,
            }
            if anchor["date"] is None:
                warnings.append(f"verification anchor without date: {raw_content!r}")
            verification_anchors.append(anchor)
            continue

        near_hit = next(
            (header for header, pat in _NEAR_MISS_PATTERNS if pat.match(raw_line)),
            None,
        )
        if near_hit is not None:
            any_marker = True
            warnings.append(
                f"near-miss heading '{near_hit}:' is not a recognized precedent "
                f"section header (expected one of {SECTION_HEADERS})"
            )
            continue

        if current_section is not None:
            m_item = _ITEM_RE.match(raw_line)
            if m_item:
                item_text = m_item.group(1).strip()
                if current_section in ("却下案", "隣接確認"):
                    alternative, item_reason, has_sep = _split_rejected_item(item_text)
                    if not has_sep:
                        if current_section == "却下案":
                            warnings.append(
                                f"rejected alternative without ': ' separator: {item_text!r}"
                            )
                        else:
                            warnings.append(
                                f"adjacent check item without ': ' separator: {item_text!r}"
                            )
                    if current_section == "却下案":
                        current_items.append(
                            {"alternative": alternative, "reason": item_reason}
                        )
                    else:
                        current_items.append(
                            {"axis": alternative, "note": item_reason}
                        )
                else:
                    current_items.append(item_text)
            # 節本文中の箇条書き以外の行（空行含む）は節を閉じずに無視する

    _close_section()

    if not any_marker:
        return None

    return {
        "rejected_alternatives": rejected_alternatives,
        "scope_in": scope_in,
        "scope_out": scope_out,
        "verification_anchors": verification_anchors,
        "adjacent_check": adjacent_check,
        "warnings": warnings,
    }


def summarize_precedent(parsed: dict) -> dict:
    """parse_precedent_sections の結果からレスポンス添付用のコンパクト形を作る。

    Args:
        parsed: parse_precedent_sections が返した dict（None ではないもの）。

    Returns:
        {
          "rejected_alternatives": <件数>,
          "scope": <適用条件/適用外のいずれかが非空か bool>,
          "verification_anchors": [<raw 文字列>, ...],   # 生テキストのまま。空リスト=決定のみ
          "adjacent_check": [<"軸: 内容" 文字列>, ...],   # 生テキスト表現。空リスト=節なし
          "warnings": [str, ...],   # 書式崩れがある場合のみ付与（無ければキー自体を省く）
        }
    """
    compact = {
        "rejected_alternatives": len(parsed.get("rejected_alternatives") or []),
        "scope": bool(parsed.get("scope_in")) or bool(parsed.get("scope_out")),
        "verification_anchors": [
            anchor["raw"] for anchor in (parsed.get("verification_anchors") or [])
        ],
        "adjacent_check": [
            f"{entry['axis']}: {entry['note']}" if entry.get("note") else entry["axis"]
            for entry in (parsed.get("adjacent_check") or [])
        ],
    }
    # 書式崩れは書き手にフィードバックする経路が無いと気づけないため、warning が
    # あるときだけコンパクト形にも載せて読み出し面（get_decisions / get_by_ids）へ
    # 露出する。崩れの無い precedent は 4 キーのまま（legacy との差分を最小化）。
    warnings = parsed.get("warnings") or []
    if warnings:
        compact["warnings"] = list(warnings)
    return compact


def attach_precedent(item: dict, reason: str | None) -> None:
    """reason に定型節があれば item["precedent"] にコンパクト形を付与する（in-place）。

    節が無い（parse が None を返す）場合はキーを付けない（legacy 本文と規約準拠本文を
    区別できるようにする）。読み出し面の付与手順を 1 箇所に集約するためのヘルパーで、
    decision の item 組み立て側はこれを呼ぶだけにする。
    """
    parsed = parse_precedent_sections(reason or "")
    if parsed is not None:
        item["precedent"] = summarize_precedent(parsed)
