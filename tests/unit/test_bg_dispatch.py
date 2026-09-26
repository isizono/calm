"""scripts/bg_dispatch.py の単体テスト。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.bg_dispatch import build_request, main  # noqa: E402

_PARENT_KWARGS = {"parent_goal_handle": "orch-goal", "parent_condition_id": 12}


def _build(**overrides):
    kwargs = {
        "activity_id": 1,
        "activity_title": "a",
        "worktree": "/w",
        **_PARENT_KWARGS,
    }
    kwargs.update(overrides)
    return build_request(**kwargs)


class TestBuildRequest:
    def test_required_fields_are_embedded(self):
        text = build_request(
            activity_id=42,
            activity_title="[作業] テスト",
            worktree="/path/to/worktree",
            parent_goal_handle="orch-goal",
            parent_condition_id=12,
        )
        assert "activity_id=42" in text
        assert "[作業] テスト" in text
        assert "/path/to/worktree" in text

    def test_branch_omitted_when_not_given(self):
        text = _build()
        assert "ブランチ" not in text

    def test_branch_included_when_given(self):
        text = _build(branch="feature/x")
        assert "ブランチ feature/x" in text

    def test_completion_and_dont_items_appended_as_bullets(self):
        text = _build(completion=["CIが緑になった"], dont=["デプロイ"])
        assert "- CIが緑になった" in text
        assert "- デプロイ" in text

    def test_goal_handle_given_instructs_get_goal(self):
        text = _build(goal_handle="my-goal-handle")
        assert 'get_goal(handle="my-goal-handle")' in text
        assert "set_goalで書く" not in text

    def test_goal_handle_omitted_instructs_set_goal_fallback(self):
        text = _build()
        assert "goalが未定義なら、スコープを条件としてset_goalで書く" in text

    def test_custom_sync_memory_scope(self):
        text = _build(sync_memory_scope="sync-memory")
        assert "`sync-memory`" in text
        assert "sync-memory --minimal" not in text

    def test_sync_memory_runs_before_final_report(self):
        text = _build()
        assert "最後の報告の前に" in text

    def test_parent_check_step_always_present(self):
        text = _build(
            activity_id=9, parent_goal_handle="parent-handle", parent_condition_id=55,
        )
        assert 'get_goal(handle="parent-handle")' in text
        assert "id_raw=55の条件のboundが" in text
        assert '{"type": "activity", "id_raw": 9}' in text
        assert "親goalのactivitiesの1件目にあるアクティビティへadd_logsで理由を書いて止める" in text

    def test_report_destination_is_derived_not_a_session_name(self):
        text = _build()
        assert "以降の報告先(親のorchアクティビティ)として控える" in text
        assert "3・4で控えた親のorchアクティビティへadd_logsで報告を書く" in text

    def test_notify_living_holder_and_skip_when_absent(self):
        text = _build()
        assert "その行のnameへSendMessageで" in text
        assert "空席・死んでいる・送れないときは知らせを省く" in text

    def test_outward_facing_boundary_lines_present(self):
        text = _build()
        assert "外向きの操作" in text
        assert "~/.claude配下の変更" in text
        assert "止められたら迂回しない" in text
        assert "人間宛てのaskは起票しない" in text
        assert "EnterWorktreeは使わない" in text

    def test_proceed_without_waiting_lines_present(self):
        text = _build()
        assert "返事を待たずに最も妥当な方針で進め" in text

    def test_migration_number_is_taken_not_declared(self):
        text = _build()
        assert "origin/mainとopen PRの番号の最大値+1を取る" in text
        assert "宣言して返事を待たない" in text

    def test_pending_dir_arg_is_embedded(self):
        text = _build(pending_dir="/tmp/calm-pending")
        assert "`/tmp/calm-pending` へファイルとして退避し、報告に書く" in text

    def test_pending_dir_falls_back_to_env_var(self, monkeypatch):
        monkeypatch.setenv("CALM_PENDING_DIR", "/tmp/from-env")
        text = _build()
        assert "`/tmp/from-env` へファイルとして退避し、報告に書く" in text

    def test_pending_dir_arg_takes_precedence_over_env_var(self, monkeypatch):
        monkeypatch.setenv("CALM_PENDING_DIR", "/tmp/from-env")
        text = _build(pending_dir="/tmp/from-arg")
        assert "/tmp/from-arg" in text
        assert "/tmp/from-env" not in text

    def test_pending_dir_generic_fallback_when_unset(self, monkeypatch):
        monkeypatch.delenv("CALM_PENDING_DIR", raising=False)
        text = _build()
        assert "退避先の設定なし" in text


class TestMainCli:
    def test_main_prints_request_with_all_args(self, capsys):
        main([
            "--activity-id", "7",
            "--activity-title", "テスト活動",
            "--worktree", "/tmp/wt",
            "--branch", "feature/y",
            "--completion", "テストを回す",
            "--dont", "マージ",
            "--parent-goal-handle", "orch-goal",
            "--parent-condition-id", "12",
        ])
        out = capsys.readouterr().out
        assert "activity_id=7" in out
        assert "テスト活動" in out
        assert "/tmp/wt" in out
        assert "feature/y" in out
        assert "- テストを回す" in out

    def test_main_includes_parent_check_and_pending_dir(self, capsys):
        main([
            "--activity-id", "7",
            "--activity-title", "テスト活動",
            "--worktree", "/tmp/wt",
            "--parent-goal-handle", "orch-goal",
            "--parent-condition-id", "12",
            "--pending-dir", "/tmp/pending",
        ])
        out = capsys.readouterr().out
        assert 'get_goal(handle="orch-goal")' in out
        assert "id_raw=12の条件のboundが" in out
        assert "/tmp/pending" in out

    def test_main_requires_parent_goal_handle(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main([
                "--activity-id", "7",
                "--activity-title", "テスト活動",
                "--worktree", "/tmp/wt",
                "--parent-condition-id", "12",
            ])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "--parent-goal-handle" in err

    def test_main_requires_parent_condition_id(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main([
                "--activity-id", "7",
                "--activity-title", "テスト活動",
                "--worktree", "/tmp/wt",
                "--parent-goal-handle", "orch-goal",
            ])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "--parent-condition-id" in err
