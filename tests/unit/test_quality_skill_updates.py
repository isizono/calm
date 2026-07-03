"""品質投資コンポーネント（バックアップ / シグナル吸い上げ / 小PR化支援）に接続する
skill文面・spec docsの契約テスト。

既存の test_worker_skill_md.py / test_audit_skill_md.py と同じパターンで、
SKILL.md / spec docs 本文に必要な記述が存在することをassertする。
"""
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

TASK_PLAN_SKILL_MD = _REPO_ROOT / ".claude" / "skills" / "task-plan" / "SKILL.md"
TASK_EXECUTE_SKILL_MD = _REPO_ROOT / ".claude" / "skills" / "task-execute" / "SKILL.md"
RECORDING_SKILL_MD = _REPO_ROOT / "skills" / "recording" / "SKILL.md"
WORKER_SYNC_SKILL_MD = _REPO_ROOT / "skills" / "worker-sync" / "SKILL.md"
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
        assert "migration lint 宣言要否" in content
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


class TestWorkerSyncSkillSignalCheck:
    def test_signal_report_step_exists(self):
        content = _read(WORKER_SYNC_SKILL_MD)
        assert "report_signal" in content

    def test_steps_renumbered_without_gaps(self):
        content = _read(WORKER_SYNC_SKILL_MD)
        assert "### 3. signal報告の確認" in content
        assert "### 4. decisionの扱い" in content
        assert "### 5. 完了" in content
        # 旧採番 "### 3. decisionの扱い" / "### 4. 完了" が残っていない
        assert "### 3. decisionの扱い" not in content
        assert "### 4. 完了" not in content


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
        assert "### 2.32 report_signal" in content
        assert "### 2.33 get_signals" in content
        assert "### 2.34 update_signal" in content

    def test_tool_count_updated(self):
        content = _read(MCP_TOOLS_SPEC)
        assert "全39ツール" in content
        assert "全36ツール" not in content


class TestDbSchemaSpecSignalEventsTable:
    def test_signal_events_listed_in_table_overview(self):
        content = _read(DB_SCHEMA_SPEC)
        assert "`signal_events`" in content

    def test_signal_events_detail_section_exists(self):
        content = _read(DB_SCHEMA_SPEC)
        assert "### 3.18 signal_events" in content

    def test_signal_events_migration_reference(self):
        content = _read(DB_SCHEMA_SPEC)
        assert "0049_add_signal_events" in content
