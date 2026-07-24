"""spec docのツール総数が実装と一致することを検証する導出型整合性lint。"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SRC_MAIN = _REPO_ROOT / "src" / "main.py"
MCP_TOOLS_SPEC = _REPO_ROOT / "docs" / "spec" / "mcp-tools.md"


def _read(path: Path) -> str:
    assert path.exists(), f"ファイルが存在しない: {path}"
    return path.read_text(encoding="utf-8")


class TestMcpToolsSpecToolCount:
    """spec docのツール総数が src/main.py の実装（@mcp.tool 実カウント）と一致すること。"""

    def _actual_tool_count(self) -> int:
        content = _read(SRC_MAIN)
        return len(re.findall(r"@mcp\.tool\(", content))

    def _declared_tool_count(self) -> int:
        content = _read(MCP_TOOLS_SPEC)
        match = re.search(r"全(\d+)ツール", content)
        assert match is not None, "spec docに『全Nツール』の記載が見つからない"
        return int(match.group(1))

    def test_declared_count_matches_impl(self):
        assert self._declared_tool_count() == self._actual_tool_count()

    def test_previously_missing_tools_documented(self):
        content = _read(MCP_TOOLS_SPEC)
        # カテゴリ一覧と詳細セクションの両方に記載されていること
        for tool in ("export_material",):
            assert f"`{tool}`" in content, f"{tool} がカテゴリ一覧に無い"
            assert re.search(rf"^### 2\.\d+ .*\b{tool}\b", content, re.MULTILINE), (
                f"{tool} の詳細セクションが無い"
            )
