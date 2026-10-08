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
_HOLDER_KWARGS = {
    "holder_name": "holder-x",
    "holder_session_id": "sess-123",
    "holder_transcript": "/t/sess-123.jsonl",
}


def _build(**overrides):
    kwargs = {
        "activity_id": 1,
        "activity_title": "a",
        "worktree": "/w",
        **_PARENT_KWARGS,
    }
    if overrides.get("role") == "consultant":
        kwargs.update(_HOLDER_KWARGS)
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

    def test_parent_check_step_embeds_given_values(self):
        text = _build(
            activity_id=9, parent_goal_handle="parent-handle", parent_condition_id=55,
        )
        assert 'get_goal(handle="parent-handle")' in text
        assert "id_raw=55の条件のboundが" in text
        assert '{"type": "activity", "id_raw": 9}' in text

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

    def test_role_switches_template(self):
        assert _build(role="consultant") != _build()

    def test_consultant_activity_id_value_is_embedded(self):
        text77 = _build(consultant_activity_id=77)
        assert "activity_id=77" in text77
        assert text77 != _build(consultant_activity_id=88)

    def test_consultant_section_removal_restores_default_text(self):
        with_consultant = _build(consultant_activity_id=77)
        before, rest = with_consultant.split("\n\n## 相談先", 1)
        after = rest.split("\n\n## やらないこと", 1)[1]
        assert before + "\n\n## やらないこと" + after == _build()

    def test_consultant_role_keeps_parent_check_values(self):
        text = _build(role="consultant", activity_id=5, parent_goal_handle="p", parent_condition_id=3)
        assert 'get_goal(handle="p")' in text
        assert "id_raw=3の条件のboundが" in text
        assert '{"type": "activity", "id_raw": 5}' in text

    @pytest.mark.parametrize("section", ["最初にやること", "やらないこと", "記録"])
    def test_shared_sections_identical_across_roles(self, section):
        kwargs = {"completion": ["c"], "dont": ["d"], "pending_dir": "/p", "activity_id": 5}

        def body(role):
            text = _build(role=role, **kwargs)
            return text.split(f"## {section}\n", 1)[1].split("\n\n## ", 1)[0]

        shared = body("worker")
        if section == "記録":
            # 作業役だけが持つ記録の項目を除いた共通の末尾が、相談役にも同じ形で入る
            assert shared.endswith(body("consultant").split("\n", 1)[1])
        else:
            assert shared == body("consultant")

    def test_consultant_request_has_holder_watch_section(self):
        text = _build(role="consultant")
        watch = text.split("## 担い手の見張り(常設の仕事)\n", 1)[1].split("\n\n## ", 1)[0]
        assert "holder-x(sessionId sess-123、transcript /t/sess-123.jsonl)" in watch
        # 世代交代で替わる担い手を追うため、見るたびに担い手欄から読み直す
        assert "担い手欄からsessionIdを読み直し" in watch
        assert "`notify_when_idle`で購読する" in watch
        assert "CronCreate" in watch
        assert "statusがbusyのまま、transcriptが25分以上更新されていない" in watch
        # 後継を自動で起こすのはユーザーの許可待ちなので、検知してログに書くだけ
        assert "報告先へadd_logsで判定の根拠" in watch
        assert "後継は起こさない" in watch
        assert "osascript" not in text

    def test_worker_request_has_no_holder_watch_section(self):
        assert "担い手の見張り" not in _build()
        assert "担い手の見張り" not in _build(consultant_activity_id=77)

    @pytest.mark.parametrize("missing", list(_HOLDER_KWARGS))
    def test_consultant_role_requires_each_holder_value(self, missing):
        kwargs = {k: v for k, v in _HOLDER_KWARGS.items() if k != missing}
        with pytest.raises(ValueError):
            build_request(
                activity_id=1, activity_title="a", worktree="/w", role="consultant",
                **_PARENT_KWARGS, **kwargs,
            )

    def test_worker_role_with_holder_rejected(self):
        with pytest.raises(ValueError):
            _build(holder_session_id="sess-123")

    def test_unknown_role_rejected(self):
        with pytest.raises(ValueError):
            _build(role="reviewer")

    def test_consultant_role_with_consultant_activity_id_rejected(self):
        with pytest.raises(ValueError):
            _build(role="consultant", consultant_activity_id=77)


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

    _CLI_BASE = [
        "--activity-id", "1", "--activity-title", "t", "--worktree", "/w",
        "--parent-goal-handle", "g", "--parent-condition-id", "2",
    ]
    _CLI_HOLDER = [
        "--holder-name", "holder-x", "--holder-session-id", "sess-123",
        "--holder-transcript", "/t/sess-123.jsonl",
    ]

    def test_main_role_and_consultant_id_change_output(self, capsys):
        main(self._CLI_BASE)
        default = capsys.readouterr().out
        main(self._CLI_BASE + ["--role", "consultant"] + self._CLI_HOLDER)
        assert capsys.readouterr().out != default
        main(self._CLI_BASE + ["--consultant-activity-id", "9"])
        assert "activity_id=9" in capsys.readouterr().out

    def test_main_rejects_role_consultant_with_consultant_activity_id(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(self._CLI_BASE + ["--role", "consultant", "--consultant-activity-id", "9"])
        assert exc_info.value.code == 2
        assert "--consultant-activity-id" in capsys.readouterr().err

    def test_main_consultant_embeds_holder_values(self, capsys):
        main(self._CLI_BASE + ["--role", "consultant"] + self._CLI_HOLDER)
        assert "holder-x(sessionId sess-123、transcript /t/sess-123.jsonl)" in capsys.readouterr().out

    def test_main_rejects_consultant_without_holder(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(self._CLI_BASE + ["--role", "consultant"] + self._CLI_HOLDER[:4])
        assert exc_info.value.code == 2
        assert "--holder-transcript" in capsys.readouterr().err

    def test_main_rejects_holder_for_worker(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(self._CLI_BASE + self._CLI_HOLDER)
        assert exc_info.value.code == 2
        assert "--holder-" in capsys.readouterr().err

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


@pytest.fixture
def marker_dir(tmp_path, monkeypatch):
    from hooks.hook_state import HookState

    monkeypatch.delenv("HOOK_STATE_DIR", raising=False)
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return tmp_path


def _dispatch(activity_id: int) -> None:
    main([
        "--activity-id", str(activity_id), "--activity-title", "t", "--worktree", "/w",
        "--parent-goal-handle", "g", "--parent-condition-id", "1",
    ])


def test_main_marks_only_target_activity_as_delegate(marker_dir):
    from hooks.delegate_marker import is_delegate_activity

    _dispatch(99)
    assert is_delegate_activity(99)
    assert not is_delegate_activity(98)


def test_marker_valid_within_ttl_and_expires_after(marker_dir):
    import os

    from hooks.delegate_marker import is_delegate_activity, marker_path

    _dispatch(5)
    path = marker_path(5)
    now = path.stat().st_mtime
    os.utime(path, (now - 23 * 3600, now - 23 * 3600))
    assert is_delegate_activity(5)
    os.utime(path, (now - 25 * 3600, now - 25 * 3600))
    assert not is_delegate_activity(5)


def test_marker_follows_hook_state_dir_env(marker_dir, monkeypatch):
    from hooks.delegate_marker import marker_path

    monkeypatch.setenv("HOOK_STATE_DIR", str(marker_dir / "env"))
    _dispatch(6)
    assert marker_path(6).parent == marker_dir / "env" / "delegate"
    assert marker_path(6).exists()


def test_request_still_printed_when_marker_write_fails(marker_dir, monkeypatch, capsys):
    from hooks.hook_state import HookState

    blocker = marker_dir / "blocker"
    blocker.write_text("file")
    monkeypatch.setattr(HookState, "BASE_DIR", blocker)  # 配下にmkdirできない
    _dispatch(7)
    assert "実装担当のbgセッション" in capsys.readouterr().out
