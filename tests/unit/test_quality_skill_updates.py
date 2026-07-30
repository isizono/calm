"""spec docのツール一覧が実装と一致することを検証する導出型整合性lint。"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SRC_MAIN = _REPO_ROOT / "src" / "main.py"
MCP_TOOLS_SPEC = _REPO_ROOT / "docs" / "spec" / "mcp-tools.md"


def _read(path: Path) -> str:
    assert path.exists(), f"ファイルが存在しない: {path}"
    return path.read_text(encoding="utf-8")


class TestMcpToolsSpecSync:
    """spec docの記載が src/main.py の実装（@mcp.tool 定義）と一致すること。"""

    def _actual_tool_count(self) -> int:
        content = _read(SRC_MAIN)
        return len(re.findall(r"@mcp\.tool\(", content))

    def _actual_tool_names(self) -> list[str]:
        content = _read(SRC_MAIN)
        return re.findall(r"@mcp\.tool\([^)]*\)\s*\ndef (\w+)\(", content)

    def _declared_tool_count(self) -> int:
        content = _read(MCP_TOOLS_SPEC)
        match = re.search(r"全(\d+)ツール", content)
        assert match is not None, "spec docに『全Nツール』の記載が見つからない"
        return int(match.group(1))

    def test_declared_count_matches_impl(self):
        assert self._declared_tool_count() == self._actual_tool_count()

    def test_all_tools_documented(self):
        content = _read(MCP_TOOLS_SPEC)
        tools = self._actual_tool_names()
        assert len(tools) == self._actual_tool_count(), (
            "ツール名抽出の正規表現がカウント用正規表現と一致していない"
        )
        missing_from_category_list = [
            tool for tool in tools if f"`{tool}`" not in content
        ]
        missing_detail_section = [
            tool
            for tool in tools
            if not re.search(rf"^### 2\.\d+ .*\b{tool}\b", content, re.MULTILINE)
        ]
        assert not missing_from_category_list, (
            f"カテゴリ一覧に記載が無いツール: {missing_from_category_list}"
        )
        assert not missing_detail_section, (
            f"詳細セクションが無いツール: {missing_detail_section}"
        )
