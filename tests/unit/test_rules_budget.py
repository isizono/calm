"""src.main.RULES（MCP instructions）の字数予算テスト。

MCPクライアント側で2,048字を超えると切り詰められる実態があるため、
全文が2,048字以内に収まることを回帰検知する。
"""
from src.main import RULES


def test_rules_within_budget():
    """RULES全文が2,048字以内である"""
    assert len(RULES) <= 2048, f"RULESが2,048字を超えている（実測{len(RULES)}字）"


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
    """末尾にguide skillへの導線1行がある"""
    assert "cc-memory:guide" in RULES
