"""roll_dice MCPツールのユニットテスト"""
from src.main import roll_dice

# FastMCP 3.xでは@mcp.tool()が元の関数をそのまま返す
_roll = roll_dice


class TestRollDice:
    """roll_diceの基本動作テスト"""

    def test_default_sides_range(self):
        """デフォルト（10面）で1〜10の範囲の値が返ること"""
        for _ in range(100):
            result = _roll()
            assert 1 <= result["result"] <= 10

    def test_six_sided_range(self):
        """sides=6で1〜6の範囲の値が返ること"""
        for _ in range(100):
            result = _roll(sides=6)
            assert 1 <= result["result"] <= 6

    def test_one_sided(self):
        """sides=1で1が返ること"""
        result = _roll(sides=1)
        assert result["result"] == 1
