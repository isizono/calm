"""MCPツールdocstring(description)の字数回帰テスト。

MCPサーバーのtool description/instructionsが一定の文字数を超えると切り詰められる
ことが実機検証で確認されている。src.main の @mcp.tool() docstring が安全マージンと
して1,900字以内に収まることと、全ツールの description と入力スキーマの合計が
上限内に収まることを検証する。

既知の超過項目(KNOWN_OVER_BUDGET)は本テスト新設時点で既に超過しており、削減は
本PRの対応範囲外のため xfail として明示する。超過が解消されたら一覧から名前を
外すこと(xfail(strict=True)のため、解消後も一覧に残すとテストが失敗して気づける)。
"""
import json

import pytest

from tests.helpers import all_tool_descriptions, all_tool_schemas

DOCSTRING_CHAR_BUDGET = 1900

# 全ツールの description + 入力スキーマ(JSON)の UTF-8 合計バイト数の上限。
# ツール定義はリクエストのたびにモデルの入力に載るため、1本ずつ上限内でも合計の増加は
# 応答時間とコストに効く(#803)。新設時点の実測 128,307 バイトに少し余裕を持たせた値。
# 各ツールの冒頭一文に英語キーワードを添えた分(約2,000バイト)を見込んで132,000とした。
# 超過したら、description を短くするか、増やす理由をPRに書いてこの値を引き上げること。
TOTAL_TOOL_DEFINITION_BYTES_BUDGET = 132_000

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


def test_total_tool_definitions_within_budget():
    """全ツールの description と入力スキーマの合計が上限を超えていないことを検証する。"""
    descriptions = all_tool_descriptions()
    schemas = all_tool_schemas()
    total = sum(len((desc or "").encode()) for desc in descriptions.values()) + sum(
        len(json.dumps(schema, ensure_ascii=False).encode()) for schema in schemas.values()
    )
    assert total <= TOTAL_TOOL_DEFINITION_BYTES_BUDGET, (
        f"ツール定義の合計が{TOTAL_TOOL_DEFINITION_BYTES_BUDGET}バイトを超えた: {total}バイト"
    )
