"""SessionStart注入セクションの予算宣言レジストリ + compose()。

各セクションは budget_chars（宣言予算・文字数）を持つ。builder の実出力が
budget_chars を超えた場合、固定の縮退マーカー付きハード切り詰めで必ず
budget_chars 以内に収める。

セクション間の結合には区切り文字（"\n"）を挟むため、全セクションが同時に
budget_chars ちょうどの出力をすると、結合後の実際の長さは
Σ budget_chars を区切り文字の分だけ上回りうる。そのため compose() は
結合結果自体の長さも最終防波堤として検査し、TOTAL_INJECTION_BUDGET_CHARS
を超えていればハード切り詰めする。

各セクションの budget_chars と TOTAL_INJECTION_BUDGET_CHARS は環境変数で
個別にオーバーライド可能なため、compose() は実行時にも
Σ budget_chars ≤ TOTAL_INJECTION_BUDGET_CHARS（CIゼロサムテストでも検証）を
前提として検査し、破られていれば ValueError を送出する。この前提が
成り立つ限り、compose() の返り値は常に TOTAL_INJECTION_BUDGET_CHARS 以内になる。
"""
import sqlite3
from dataclasses import dataclass
from typing import Callable

from src import config

SectionBuilder = Callable[..., str]  # (conn, session_id, source) -> str


@dataclass(frozen=True)
class Section:
    name: str  # レジストリ内の一意キー。予算超過時のハード切り詰めマーカーに
    # そのまま埋め込まれ、compose()の戻り値経由でSessionStartのadditionalContext
    # （ユーザーのセッションへ実際に注入される文字列）に含まれうる
    builder: SectionBuilder
    budget_chars: int
    priority: int  # 昇順。出力順を決める（予算の奪い合いには使わない。各セクション独立）


def total_declared_budget(sections: list[Section]) -> int:
    """Σ budget_chars を返す。CIゼロサムテスト専用の集計関数。"""
    return sum(s.budget_chars for s in sections)


def _hard_truncate(text: str, budget: int, name: str) -> str:
    marker = f"\n…（{name}セクション、宣言予算{budget}字超過のため切り詰め）\n"
    if budget <= len(marker):
        return marker[:budget]
    return text[: budget - len(marker)] + marker


def _hard_truncate_total(text: str, budget: int) -> str:
    marker = "\n…（総予算超過のため切り詰め）\n"
    if budget <= len(marker):
        return marker[:budget]
    return text[: budget - len(marker)] + marker


def compose(
    conn: sqlite3.Connection,
    session_id: str | None,
    source: str | None,
    sections: list[Section],
) -> str:
    """priority昇順にセクションを評価し、宣言予算内の文字列へ組み立てる。

    セクション単位のtry/exceptで例外を握り、そのセクションを空扱いにして残りは
    続行する（既存hookの挙動を保持）。

    Σ budget_chars が TOTAL_INJECTION_BUDGET_CHARS を超えている場合はValueErrorを
    送出する。両者は環境変数で個別にオーバーライド可能なため、実行時にも
    このモジュールの前提（Σ budget_chars ≤ TOTAL_INJECTION_BUDGET_CHARS）を検証する。
    """
    total_budget = config.TOTAL_INJECTION_BUDGET_CHARS
    declared_total = total_declared_budget(sections)
    if declared_total > total_budget:
        raise ValueError(
            f"総宣言予算(Σ budget_chars={declared_total})が"
            f"TOTAL_INJECTION_BUDGET_CHARS({total_budget})を超えている"
        )

    parts: list[str] = []
    for section in sorted(sections, key=lambda s: s.priority):
        try:
            text = section.builder(conn, session_id, source)
        except Exception:
            continue
        if not text:
            continue
        if len(text) > section.budget_chars:
            text = _hard_truncate(text, section.budget_chars, section.name)
        parts.append(text)

    result = "\n".join(parts)
    if len(result) > total_budget:
        # 各セクションはbudget_chars以内でも、区切り文字("\n")分の結合コストは
        # セクション単位の切り詰めではカバーされない。ここが最終防波堤。
        result = _hard_truncate_total(result, total_budget)
    return result
