"""MCPツールdocstring(description)の字数回帰テスト。

MCPサーバーのtool description/instructionsが一定の文字数を超えると切り詰められる
ことが実機検証で確認されている。src.main の @mcp.tool() docstring が安全マージンと
して1,900字以内に収まることを検証する。

既知の超過項目(KNOWN_OVER_BUDGET)は本テスト新設時点で既に超過しており、削減は
本PRの対応範囲外のため xfail として明示する。超過が解消されたら一覧から名前を
外すこと(xfail(strict=True)のため、解消後も一覧に残すとテストが失敗して気づける)。
"""
import pytest

from tests.helpers import all_tool_descriptions

DOCSTRING_CHAR_BUDGET = 1900

# 実測で1,900字を超えている既知のツール(本テスト新設時点の記録)。
KNOWN_OVER_BUDGET = {"search"}


def test_all_tool_docstrings_within_budget():
    """KNOWN_OVER_BUDGET以外のツールで新規の超過が発生していないことを検証する。"""
    descriptions = all_tool_descriptions()
    over_budget = {
        name: len(desc)
        for name, desc in descriptions.items()
        if desc and len(desc) > DOCSTRING_CHAR_BUDGET and name not in KNOWN_OVER_BUDGET
    }
    assert not over_budget, (
        f"{DOCSTRING_CHAR_BUDGET}字を超過したdocstringが新規に検出された: {over_budget}"
    )


@pytest.mark.xfail(strict=True, reason="既知の超過。削減は別対応")
@pytest.mark.parametrize("name", sorted(KNOWN_OVER_BUDGET))
def test_known_over_budget_docstrings_still_exceed(name):
    """KNOWN_OVER_BUDGETの各ツールが実際にまだ超過しているかを追跡する。

    解消されればこのテストがxfail→passに転じ、strict=Trueにより失敗として
    検出される(その時点でKNOWN_OVER_BUDGETから当該名を外すこと)。
    """
    descriptions = all_tool_descriptions()
    assert len(descriptions[name]) <= DOCSTRING_CHAR_BUDGET
