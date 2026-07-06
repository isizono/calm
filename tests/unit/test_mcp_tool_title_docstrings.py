"""MCP tool description (ToolSearch/エージェントから見える文面) の title 35字案内明記テスト。

実際のバリデーション上限は40字のままだが（test_title_validation.py が担保）、
AIエージェントが字数を数え間違えて超過することを防ぐため、docstring上の案内は
安全マージンとして35字以内としている。src.services.* 側の docstring には既に
「35字以内」が明記されているが、実際にエージェントへ配信される MCP tool description は
src.main の @mcp.tool() 関数の docstring から生成されるため、そちらへの転記漏れは
別途この層で検証する必要がある。
"""
from tests.helpers import all_tool_descriptions as _all_tool_descriptions


TITLE_TOOLS = [
    "add_topic",
    "add_decisions",
    "add_activity",
    "update_activity",
    "add_material",
    "update_material",
]


class TestToolDescriptionMentionsTitleLimit:
    def test_all_title_tools_mention_35_char_limit(self):
        descriptions = _all_tool_descriptions()
        missing = [
            name
            for name in TITLE_TOOLS
            if "35字以内" not in descriptions.get(name, "")
        ]
        assert not missing, f"MCP tool description に「35字以内」が欠落: {missing}"
