"""MCP tool description (ToolSearch/エージェントから見える文面) の事実誤り訂正テスト。

docstring監査で見つかった、実装との事実乖離を再発させないための回帰テスト。
主張の正しさそのもの（実装挙動）は各サービス層のテストで担保されるため、ここでは
「docstringが誤った/古い文言に戻っていないか」「正しい語彙が含まれているか」のみを見る。
"""
from tests.helpers import all_tool_descriptions as _all_tool_descriptions


class TestGetMaterialDescriptionAccuracy:
    def test_does_not_claim_checkin_includes_full_content(self):
        desc = _all_tool_descriptions()["get_material"]
        assert "check_in/get_by_idsの応答にmaterialのcontent/sourceが同梱される" not in desc

    def test_mentions_snippet_limitation(self):
        desc = _all_tool_descriptions()["get_material"]
        assert "snippet" in desc


class TestAddHabitDescriptionAccuracy:
    def test_does_not_claim_checkin_injection(self):
        desc = _all_tool_descriptions()["add_habit"]
        assert "check-in時に自動注入" not in desc

    def test_does_not_claim_session_start_injection(self):
        """habitsの常時配信経路はSessionStart hookの直接注入ではなく、
        ~/.claude/rules配下の自動生成ファイル投影である"""
        desc = _all_tool_descriptions()["add_habit"]
        assert "SessionStart時に全件注入" not in desc

    def test_mentions_rules_projection_delivery(self):
        desc = _all_tool_descriptions()["add_habit"]
        assert "~/.claude/rules" in desc
        assert "trigger_mode='always'" in desc


class TestRelationDescriptionMentionsBelongsTo:
    def test_add_relation_mentions_belongs_to(self):
        desc = _all_tool_descriptions()["add_relation"]
        assert "belongs_to" in desc

    def test_remove_relation_mentions_belongs_to(self):
        desc = _all_tool_descriptions()["remove_relation"]
        assert "belongs_to" in desc


class TestSnoozedAutoRevivalDescribed:
    def test_get_activities_mentions_snooze_duration(self):
        desc = _all_tool_descriptions()["get_activities"]
        assert "SNOOZE_DURATION_DAYS" in desc

    def test_update_activity_mentions_auto_revival(self):
        desc = _all_tool_descriptions()["update_activity"]
        assert "snoozed" in desc
        assert 'status="pending"' in desc


class TestSearchDescriptionAccuracy:
    def test_mentions_three_char_fts_threshold(self):
        desc = _all_tool_descriptions()["search"]
        assert "3文字以上" in desc

    def test_mentions_get_by_ids_for_full_text(self):
        desc = _all_tool_descriptions()["search"]
        assert "get_by_ids" in desc


class TestCheckInChooseMentionsGetDecisions:
    def test_choose_section_lists_get_decisions(self):
        desc = _all_tool_descriptions()["check_in"]
        assert "get_decisions" in desc
