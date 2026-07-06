"""品質投資コンポーネント（バックアップ / シグナル吸い上げ / 小PR化支援）に接続する
skill文面・spec docsの契約テスト。

既存の test_audit_skill_md.py と同じパターンで、
SKILL.md / spec docs 本文に必要な記述が存在することをassertする。
"""
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SRC_MAIN = _REPO_ROOT / "src" / "main.py"

TASK_PLAN_SKILL_MD = _REPO_ROOT / ".claude" / "skills" / "task-plan" / "SKILL.md"
TASK_EXECUTE_SKILL_MD = _REPO_ROOT / ".claude" / "skills" / "task-execute" / "SKILL.md"
RECORDING_SKILL_MD = _REPO_ROOT / "skills" / "recording" / "SKILL.md"
GUIDE_SKILL_MD = _REPO_ROOT / "skills" / "guide" / "SKILL.md"
MCP_TOOLS_SPEC = _REPO_ROOT / "docs" / "spec" / "mcp-tools.md"
DB_SCHEMA_SPEC = _REPO_ROOT / "docs" / "spec" / "db-schema.md"


def _read(path: Path) -> str:
    assert path.exists(), f"ファイルが存在しない: {path}"
    return path.read_text(encoding="utf-8")


class TestTaskPlanSkillSizeEstimate:
    def test_subplan_template_has_size_estimate_field(self):
        content = _read(TASK_PLAN_SKILL_MD)
        assert "## サイズ見込み" in content

    def test_single_pr_template_has_size_estimate_field(self):
        content = _read(TASK_PLAN_SKILL_MD)
        # サブプランテンプレートと単一PRテンプレートの両方に出現する（2箇所）
        assert content.count("## サイズ見込み") >= 2

    def test_type_b_subplan_has_migration_revert_fields(self):
        content = _read(TASK_PLAN_SKILL_MD)
        assert "破壊的変更 lint 該当有無" in content
        assert "revert 分類" in content
        assert "R1" in content and "R2" in content


class TestTaskExecuteSkillPrSizeCheck:
    def test_pr_creation_step_runs_local_size_check(self):
        content = _read(TASK_EXECUTE_SKILL_MD)
        assert "pr_size_check.py --local" in content

    def test_oversized_verdict_triggers_split_consideration(self):
        content = _read(TASK_EXECUTE_SKILL_MD)
        assert "oversized" in content
        assert "分割" in content

    def test_size_check_runs_before_pr_create(self):
        content = _read(TASK_EXECUTE_SKILL_MD)
        size_check_idx = content.find("pr_size_check.py --local")
        pr_create_idx = content.find("PRを作成\n")
        assert size_check_idx > 0
        assert pr_create_idx > size_check_idx


class TestRecordingSkillSignalRouting:
    def test_report_signal_routing_note_exists(self):
        content = _read(RECORDING_SKILL_MD)
        assert "report_signal" in content

    def test_kind_examples_documented(self):
        content = _read(RECORDING_SKILL_MD)
        for kind in ("machine_error", "friction", "contradiction"):
            assert kind in content

    def test_distinguishes_from_l5_bug_observation(self):
        content = _read(RECORDING_SKILL_MD)
        assert "L5" in content


class TestGuideSkillRestoreCommand:
    def test_restore_latest_command_documented(self):
        content = _read(GUIDE_SKILL_MD)
        assert "scripts/snapshot.py restore --latest" in content

    def test_list_command_documented(self):
        content = _read(GUIDE_SKILL_MD)
        assert "scripts/snapshot.py list" in content

    def test_one_command_restore_is_primary_instruction(self):
        content = _read(GUIDE_SKILL_MD)
        assert "ワンコマンドで最新のスナップショットから復元する" in content
        # 旧手順名（--latestを案内しないバージョン）が残っていない
        assert "3. 復元を実行する:" not in content


class TestMcpToolsSpecSignalSection:
    def test_signal_tool_group_listed(self):
        content = _read(MCP_TOOLS_SPEC)
        assert "シグナル系（signal_events）" in content

    def test_all_three_signal_tools_have_detail_sections(self):
        content = _read(MCP_TOOLS_SPEC)
        assert "### 2.29 report_signal" in content
        assert "### 2.30 get_signals" in content
        assert "### 2.31 update_signal" in content

    def test_tool_count_updated(self):
        content = _read(MCP_TOOLS_SPEC)
        assert "全41ツール" in content
        assert "全37ツール" not in content


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


class TestDbSchemaSpecSignalEventsTable:
    def test_signal_events_listed_in_table_overview(self):
        content = _read(DB_SCHEMA_SPEC)
        assert "`signal_events`" in content

    def test_signal_events_detail_section_exists(self):
        content = _read(DB_SCHEMA_SPEC)
        assert re.search(r"^### 3\.\d+ signal_events$", content, re.MULTILINE)

    def test_signal_events_migration_reference(self):
        content = _read(DB_SCHEMA_SPEC)
        assert "0049_add_signal_events" in content
