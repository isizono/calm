"""MCP tool description (ToolSearch/エージェントから見える文面) の title 40字上限明記テスト。

src.services.* 側の docstring には既に「40字以内」が明記されているが（test_title_validation.py が担保）、
実際にエージェントへ配信される MCP tool description は src.main の @mcp.tool() 関数の docstring から
生成されるため、そちらへの転記漏れは別途この層で検証する必要がある。
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
    def test_all_title_tools_mention_40_char_limit(self):
        descriptions = _all_tool_descriptions()
        missing = [
            name
            for name in TITLE_TOOLS
            if "40字以内" not in descriptions.get(name, "")
        ]
        assert not missing, f"MCP tool description に「40字以内」が欠落: {missing}"
