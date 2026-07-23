"""SessionStart注入セクションの予算宣言レジストリ + compose()。

各セクションは budget_chars（宣言予算・文字数）を持つ。builder の実出力が
budget_chars を超えた場合、degrade（宣言されていれば）→ それでも超えるなら
固定の縮退マーカー付きハード切り詰め、の順で必ず budget_chars 以内に収める。

セクション間の結合には区切り文字（"\n"）を挟むため、全セクションが同時に
budget_chars ちょうどの出力をすると、結合後の実際の長さは
Σ budget_chars を区切り文字の分だけ上回りうる。そのため compose() は
結合結果自体の長さも最終防波堤として検査し、TOTAL_INJECTION_BUDGET_CHARS
を超えていればハード切り詰めする。これにより Σ budget_chars と
TOTAL_INJECTION_BUDGET_CHARS の関係（CIゼロサムテストで検証）に関わらず、
compose() の返り値は常に TOTAL_INJECTION_BUDGET_CHARS 以内になる。
"""
from dataclasses import dataclass
from typing import Callable, Optional

from src import config

SectionBuilder = Callable[..., str]  # (conn, session_id, source) -> str


@dataclass(frozen=True)
class Section:
    name: str  # レジストリ内の一意キー。ログ・テスト識別用。ユーザー表示はしない
    builder: SectionBuilder
    budget_chars: int
    priority: int  # 昇順。出力順を決める（予算の奪い合いには使わない。各セクション独立）
    degrade: Optional[Callable[[str], str]] = None
    # degrade(overflow_text) -> budget_chars以内に収まるはずの代替テキスト。
    # Noneならハード切り詰めのみ行う。degrade自体がbudget_chars以内を保証しない
    # 実装ミスにも備え、compose側で常に再チェックする。


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


def compose(conn, session_id, source, sections: list[Section]) -> str:
    """priority昇順にセクションを評価し、宣言予算内の文字列へ組み立てる。

    セクション単位のtry/exceptで例外を握り、そのセクションを空扱いにして残りは
    続行する（既存hookの挙動を保持）。
    """
    parts: list[str] = []
    for section in sorted(sections, key=lambda s: s.priority):
        try:
            text = section.builder(conn, session_id, source)
        except Exception:
            continue
        if not text:
            continue
        if len(text) > section.budget_chars:
            if section.degrade is not None:
                try:
                    text = section.degrade(text)
                except Exception:
                    text = _hard_truncate(text, section.budget_chars, section.name)
            else:
                text = _hard_truncate(text, section.budget_chars, section.name)
            if len(text) > section.budget_chars:
                # degradeがbudget_chars以内に収め損ねた場合の最終防波堤
                text = _hard_truncate(text, section.budget_chars, section.name)
        parts.append(text)

    result = "\n".join(parts)
    total_budget = config.TOTAL_INJECTION_BUDGET_CHARS
    if len(result) > total_budget:
        # 各セクションはbudget_chars以内でも、区切り文字("\n")分の結合コストは
        # セクション単位の切り詰めではカバーされない。ここが最終防波堤。
        result = _hard_truncate_total(result, total_budget)
    return result
