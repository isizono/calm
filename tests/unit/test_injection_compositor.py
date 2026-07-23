"""src/services/injection_compositor.py のユニットテスト

DB不要（ダミーのbuilder関数のみで完結する純ロジックテスト）。
compose()の予算管理契約（priority順・try/except・degrade・ハード切り詰め）を
実DB経由のhookテスト(tests/e2e/test_session_start_hook.py)とは独立に検証する。
"""
from src import config
from src.services.injection_compositor import Section, compose, total_declared_budget


def _const_builder(text: str):
    def _builder(conn, session_id, source):
        return text
    return _builder


class TestComposeOrdering:
    def test_sections_joined_in_priority_order(self):
        sections = [
            Section("b", _const_builder("second"), budget_chars=100, priority=20),
            Section("a", _const_builder("first"), budget_chars=100, priority=10),
        ]
        result = compose(None, None, None, sections)

        assert result == "first\nsecond"

    def test_empty_section_output_omitted(self):
        sections = [
            Section("empty", _const_builder(""), budget_chars=100, priority=0),
            Section("present", _const_builder("content"), budget_chars=100, priority=10),
        ]
        result = compose(None, None, None, sections)

        assert result == "content"


class TestComposeExceptionIsolation:
    def test_one_section_exception_does_not_break_others(self):
        def _boom(conn, session_id, source):
            raise RuntimeError("boom")

        sections = [
            Section("boom", _boom, budget_chars=100, priority=0),
            Section("ok", _const_builder("survivor"), budget_chars=100, priority=10),
        ]
        result = compose(None, None, None, sections)

        assert result == "survivor"


class TestComposeBudgetEnforcement:
    def test_output_within_budget_passes_through_unchanged(self):
        sections = [Section("s", _const_builder("short text"), budget_chars=100, priority=0)]
        result = compose(None, None, None, sections)

        assert result == "short text"

    def test_overflow_without_degrade_is_hard_truncated_within_budget(self):
        budget = 60
        sections = [Section("s", _const_builder("x" * 1000), budget_chars=budget, priority=0)]
        result = compose(None, None, None, sections)

        assert len(result) <= budget
        assert "切り詰め" in result

    def test_degrade_return_value_is_used_when_within_budget(self):
        def _degrade(overflow_text: str) -> str:
            return "degraded"

        sections = [
            Section(
                "s",
                _const_builder("x" * 1000),
                budget_chars=50,
                priority=0,
                degrade=_degrade,
            )
        ]
        result = compose(None, None, None, sections)

        assert result == "degraded"

    def test_degrade_exception_falls_back_to_hard_truncate(self):
        def _degrade(overflow_text: str) -> str:
            raise RuntimeError("degrade blew up")

        budget = 60
        sections = [
            Section(
                "s",
                _const_builder("x" * 1000),
                budget_chars=budget,
                priority=0,
                degrade=_degrade,
            )
        ]
        result = compose(None, None, None, sections)

        assert len(result) <= budget
        assert "切り詰め" in result

    def test_degrade_still_over_budget_is_hard_truncated_again(self):
        """degrade実装バグ（budget_chars以内に収め損ねる）への耐性確認"""
        def _degrade(overflow_text: str) -> str:
            return "y" * 1000  # わざと予算を超える戻り値

        budget = 60
        sections = [
            Section(
                "s",
                _const_builder("x" * 1000),
                budget_chars=budget,
                priority=0,
                degrade=_degrade,
            )
        ]
        result = compose(None, None, None, sections)

        assert len(result) <= budget
        assert "切り詰め" in result


class TestComposeTotalBudgetEnforcement:
    """セクション結合時の区切り文字("\n")がΣbudget_charsに含まれていなかった
    バグの回帰テスト。各セクションがbudget_chars以内でも、結合後の実際の長さは
    区切り文字の分だけΣbudget_charsを上回りうる。
    """

    def test_join_separator_overflow_is_truncated_within_total_budget(self, monkeypatch):
        """5セクションが宣言予算ちょうど（合計500字）を出力し、区切り文字4個分
        （4字）だけΣbudget_charsを超える最小再現。TOTAL_INJECTION_BUDGET_CHARSを
        Σbudget_charsと同じ500に固定し、余裕ゼロの状況を作る。
        """
        monkeypatch.setattr(config, "TOTAL_INJECTION_BUDGET_CHARS", 500)
        budget_each = 100
        sections = [
            Section(f"s{i}", _const_builder("x" * budget_each), budget_chars=budget_each, priority=i)
            for i in range(5)
        ]

        result = compose(None, None, None, sections)

        assert len(result) <= config.TOTAL_INJECTION_BUDGET_CHARS
        assert "切り詰め" in result

    def test_all_six_sections_at_config_budget_cap_stays_within_total(self):
        """レビューで実機再現されたシナリオ: config.pyの6セクション予算合計は
        TOTAL_INJECTION_BUDGET_CHARSとちょうど一致し余裕がない。全セクションが
        同時に宣言予算ちょうどの出力をすると、5個の"\n"区切り文字分だけ
        実測がTOTAL_INJECTION_BUDGET_CHARSを超えていた（修正前は10,005字）。
        """
        budgets = [
            config.INJECTION_BUDGET_SNAPSHOT_CHARS,
            config.INJECTION_BUDGET_ACTIVITIES_CHARS,
            config.INJECTION_BUDGET_HABITS_CHARS,
            config.INJECTION_BUDGET_SYNC_POLICY_CHARS,
            config.INJECTION_BUDGET_SIGNALS_CHARS,
            config.INJECTION_BUDGET_RELAY_INBOX_CHARS,
        ]
        sections = [
            Section(f"s{i}", _const_builder("x" * budget), budget_chars=budget, priority=i)
            for i, budget in enumerate(budgets)
        ]

        result = compose(None, None, None, sections)

        assert len(result) <= config.TOTAL_INJECTION_BUDGET_CHARS


class TestTotalDeclaredBudget:
    def test_sums_budget_chars_across_sections(self):
        sections = [
            Section("a", _const_builder(""), budget_chars=100, priority=0),
            Section("b", _const_builder(""), budget_chars=250, priority=1),
        ]

        assert total_declared_budget(sections) == 350
