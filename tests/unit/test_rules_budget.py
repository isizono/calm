"""src.main.RULES（MCP instructions）の字数予算テスト。

MCPクライアント側で2,048字を超えると切り詰められる実態があるため、
全文がハードリミット2,048字以内に収まることを回帰検知する。

tests/unit/test_tool_docstring_budget.pyと同じ設計思想で、安全マージン
1,900字も別テストで追跡する。RULESは本テスト更新時点で実測2,015字あり、
安全マージンを既に超えている（既知の超過としてxfail(strict=True)で
追跡。マージン内に削減されたらxfailがxpassに転じ、strict=Trueにより
失敗として検出される）。
"""
import pytest

from src.main import RULES

RULES_HARD_LIMIT = 2048
RULES_SAFE_BUDGET = 1900


def test_rules_within_budget():
    """RULES全文がハードリミット2,048字以内である"""
    assert len(RULES) <= RULES_HARD_LIMIT, (
        f"RULESが{RULES_HARD_LIMIT}字を超えている（実測{len(RULES)}字）"
    )


@pytest.mark.xfail(strict=True, reason="既知の超過。安全マージンへの削減は別対応")
def test_rules_within_safe_budget():
    """RULES全文が安全マージン1,900字以内である

    解消されればこのテストがxfail→passに転じ、strict=Trueにより失敗として
    検出される(その時点でxfailマーカーを外すこと)。
    """
    assert len(RULES) <= RULES_SAFE_BUDGET


def test_rules_contains_context_retrieval_principle():
    """最初の応答前に関連記録を取得する原則が含まれる"""
    assert "最初の応答を組み立てる前に" in RULES


def test_rules_contains_activity_check_in_guidance():
    """作業時はactivity作成+check_inする旨が含まれる"""
    assert "check_in" in RULES
    assert "アクティビティを作成" in RULES


def test_rules_contains_decision_log_material_distinction():
    """decision/log/materialの使い分け要点が含まれる"""
    assert "add_decisions" in RULES
    assert "add_logs" in RULES
    assert "add_material" in RULES


def test_rules_contains_required_tags():
    """domain:/intent:タグが必須である旨が含まれる"""
    assert "domain:" in RULES
    assert "intent:" in RULES


def test_rules_ends_with_guide_skill_pointer():
    """末尾にman skillへの導線1行がある"""
    assert "calm:man" in RULES
