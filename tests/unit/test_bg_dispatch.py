"""scripts/bg_dispatch.py の単体テスト。"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.bg_dispatch import build_request, main  # noqa: E402


class TestBuildRequest:
    def test_required_fields_are_embedded(self):
        text = build_request(
            activity_id=42,
            activity_title="[作業] テスト",
            worktree="/path/to/worktree",
            report_to="orch",
        )
        assert "activity_id=42" in text
        assert "[作業] テスト" in text
        assert "/path/to/worktree" in text
        assert "orch" in text

    def test_fixed_skeleton_items_present(self):
        """今日書かれた依頼文4本から抜き出した固定の骨格が全て含まれる。"""
        text = build_request(
            activity_id=1, activity_title="a", worktree="/w", report_to="orch",
        )
        assert "check_in" in text
        assert "goal.next" in text
        assert "マージ" in text
        assert "サーバー再起動" in text
        assert "マイグレーション番号を勝手に取ること" in text
        assert "add_logs" in text
        assert "SendMessage" in text
        assert "sync-memory" in text

    def test_branch_omitted_when_not_given(self):
        text = build_request(
            activity_id=1, activity_title="a", worktree="/w", report_to="orch",
        )
        assert "ブランチ" not in text

    def test_branch_included_when_given(self):
        text = build_request(
            activity_id=1, activity_title="a", worktree="/w", report_to="orch",
            branch="feature/x",
        )
        assert "ブランチ feature/x" in text

    def test_completion_and_dont_items_appended_as_bullets(self):
        text = build_request(
            activity_id=1, activity_title="a", worktree="/w", report_to="orch",
            completion=["CIが緑になった"], dont=["デプロイ"],
        )
        assert "- CIが緑になった" in text
        assert "- デプロイ" in text

    def test_goal_handle_given_instructs_get_goal(self):
        text = build_request(
            activity_id=1, activity_title="a", worktree="/w", report_to="orch",
            goal_handle="my-goal-handle",
        )
        assert 'get_goal(handle="my-goal-handle")' in text
        assert "set_goalで書く" not in text

    def test_goal_handle_omitted_instructs_set_goal_fallback(self):
        text = build_request(
            activity_id=1, activity_title="a", worktree="/w", report_to="orch",
        )
        assert "goalが未定義なら、スコープを条件としてset_goalで書く" in text

    def test_custom_sync_memory_scope(self):
        text = build_request(
            activity_id=1, activity_title="a", worktree="/w", report_to="orch",
            sync_memory_scope="sync-memory",
        )
        assert "`sync-memory`" in text
        assert "sync-memory --minimal" not in text


class TestMainCli:
    def test_main_prints_request_with_all_args(self, capsys):
        main([
            "--activity-id", "7",
            "--activity-title", "テスト活動",
            "--worktree", "/tmp/wt",
            "--report-to", "orch",
            "--branch", "feature/y",
            "--completion", "テストを回す",
            "--dont", "マージ",
        ])
        out = capsys.readouterr().out
        assert "activity_id=7" in out
        assert "テスト活動" in out
        assert "/tmp/wt" in out
        assert "feature/y" in out
        assert "- テストを回す" in out
