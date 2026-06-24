"""preblock_hook.py の単体テスト。

PreToolUse hook の入出力 (stdin から event JSON 受領、stdout に
permissionDecision JSON 出力 or 空 dict 出力) と、各補助関数の挙動を検証する。

検証対象:
- _scan_text_for_literals: 1 文字列内の code / fullword 検出、エスケープ無視
- _scan_tool_input: dict / list 再帰スキャン、field path 保持
- _is_allowed: allowlist 判定 (prefix / exact match)
- _is_in_cc_memory_project: pyproject.toml 上方向探索
- main: stdin event → block / pass 判定の総合フロー
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

# hooks ディレクトリを sys.path に通して直接 import 可能にする
_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import preblock_hook  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# _scan_text_for_literals
# ---------------------------------------------------------------------------


class TestScanTextForLiterals:
    def test_code_form_detected(self):
        assert preblock_hook._scan_text_for_literals("see M#1") == ["M#1"]

    def test_fullword_form_detected(self):
        assert preblock_hook._scan_text_for_literals("see log #1") == ["log #1"]

    def test_case_insensitive_fullword(self):
        assert preblock_hook._scan_text_for_literals("LOG #1 Log #2") == [
            "LOG #1",
            "Log #2",
        ]

    def test_mixed_code_and_fullword(self):
        result = preblock_hook._scan_text_for_literals("M#1 and log #2")
        assert "M#1" in result
        assert "log #2" in result

    def test_lowercase_code_not_matched(self):
        assert preblock_hook._scan_text_for_literals("m#1 is junk") == []

    def test_escape_code_not_matched(self):
        assert preblock_hook._scan_text_for_literals("\\M#1 is literal") == []

    def test_escape_fullword_not_matched(self):
        assert preblock_hook._scan_text_for_literals("\\log #1 is literal") == []

    def test_word_boundary_blog_not_match(self):
        assert preblock_hook._scan_text_for_literals("blog #1 was published") == []

    def test_word_boundary_trailing_alnum_not_match(self):
        assert preblock_hook._scan_text_for_literals("M#1abc is junk") == []

    def test_empty_string(self):
        assert preblock_hook._scan_text_for_literals("") == []


# ---------------------------------------------------------------------------
# _scan_tool_input
# ---------------------------------------------------------------------------


class TestScanToolInput:
    def test_top_level_string_value(self):
        # root は dict 想定だが string 単独でも walk は耐える
        result = preblock_hook._scan_tool_input({"command": "echo M#1"})
        assert result == [{"match": "M#1", "field": "command"}]

    def test_nested_dict(self):
        result = preblock_hook._scan_tool_input(
            {"outer": {"inner": "see log #1"}}
        )
        assert result == [{"match": "log #1", "field": "outer.inner"}]

    def test_list_value(self):
        result = preblock_hook._scan_tool_input(
            {"args": ["echo M#1", "echo D#2"]}
        )
        assert {"match": "M#1", "field": "args[0]"} in result
        assert {"match": "D#2", "field": "args[1]"} in result

    def test_multiple_matches_in_one_field(self):
        result = preblock_hook._scan_tool_input(
            {"command": "echo M#1 and D#2"}
        )
        fields = {(r["match"], r["field"]) for r in result}
        assert ("M#1", "command") in fields
        assert ("D#2", "command") in fields

    def test_no_match_returns_empty(self):
        assert preblock_hook._scan_tool_input({"command": "echo hello"}) == []

    def test_non_string_values_ignored(self):
        result = preblock_hook._scan_tool_input(
            {"timeout": 30, "force": True, "tags": None}
        )
        assert result == []


# ---------------------------------------------------------------------------
# _is_allowed
# ---------------------------------------------------------------------------


class TestIsAllowed:
    @pytest.mark.parametrize(
        "tool_name",
        [
            "Read",
            "Grep",
            "Glob",
            "WebFetch",
            "WebSearch",
            "TaskCreate",
            "TaskGet",
            "TaskUpdate",
            "TaskList",
            "TaskStop",
            "TaskOutput",
            "Skill",
            "ToolSearch",
            "ScheduleWakeup",
            "EnterPlanMode",
            "ExitPlanMode",
            "Monitor",
            "NotebookEdit",
            "PushNotification",
        ],
    )
    def test_exact_match_allowed(self, tool_name):
        assert preblock_hook._is_allowed(tool_name) is True

    def test_cc_memory_mcp_prefix_allowed(self):
        assert preblock_hook._is_allowed(
            "mcp__plugin_claude-code-memory_cc-memory__search"
        )
        assert preblock_hook._is_allowed(
            "mcp__plugin_claude-code-memory_cc-memory__add_logs"
        )

    @pytest.mark.parametrize(
        "tool_name",
        [
            "Bash",
            "Edit",
            "Write",
            "MultiEdit",
            "Agent",
            "SendMessage",
            "TodoWrite",
            "mcp__other_namespace__something",
        ],
    )
    def test_block_target_not_allowed(self, tool_name):
        assert preblock_hook._is_allowed(tool_name) is False


# ---------------------------------------------------------------------------
# _is_in_cc_memory_project
# ---------------------------------------------------------------------------


class TestIsInCcMemoryProject:
    def test_pyproject_with_cc_memory_name(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "cc-memory"\n'
        )
        monkeypatch.chdir(tmp_path)
        assert preblock_hook._is_in_cc_memory_project() is True

    def test_pyproject_with_literal_string(self, tmp_path, monkeypatch):
        # TOML literal string (single quotes) も同様に受理される
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'cc-memory'\n"
        )
        monkeypatch.chdir(tmp_path)
        assert preblock_hook._is_in_cc_memory_project() is True

    def test_pyproject_with_other_name(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "other-project"\n'
        )
        monkeypatch.chdir(tmp_path)
        assert preblock_hook._is_in_cc_memory_project() is False

    def test_pyproject_with_name_only_in_comment(self, tmp_path, monkeypatch):
        # コメント行に `name = "cc-memory"` があっても [project].name は別ならば False
        (tmp_path / "pyproject.toml").write_text(
            '[project]\n# name = "cc-memory"\nname = "other-project"\n'
        )
        monkeypatch.chdir(tmp_path)
        assert preblock_hook._is_in_cc_memory_project() is False

    def test_pyproject_with_name_in_other_table(self, tmp_path, monkeypatch):
        # 別 table の name が "cc-memory" でも [project].name でなければ False
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "other-project"\n\n'
            '[tool.foo]\nname = "cc-memory"\n'
        )
        monkeypatch.chdir(tmp_path)
        assert preblock_hook._is_in_cc_memory_project() is False

    def test_pyproject_without_project_table(self, tmp_path, monkeypatch):
        # [project] table が無ければ False (defensive)
        (tmp_path / "pyproject.toml").write_text(
            '[tool.foo]\nname = "cc-memory"\n'
        )
        monkeypatch.chdir(tmp_path)
        assert preblock_hook._is_in_cc_memory_project() is False

    def test_pyproject_invalid_toml(self, tmp_path, monkeypatch):
        # 壊れた TOML はパース失敗 → False
        (tmp_path / "pyproject.toml").write_text("this is = = not toml [[[\n")
        monkeypatch.chdir(tmp_path)
        assert preblock_hook._is_in_cc_memory_project() is False

    def test_no_pyproject_in_tree(self, tmp_path, monkeypatch):
        # tmp_path 配下に pyproject.toml がない
        subdir = tmp_path / "sub"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        # tmp_path 上にも pyproject.toml がないことを保証するため、
        # macOS / Linux の / までさかのぼると別の pyproject に当たる可能性があるが、
        # その場合は cc-memory ではないという結果になる (False) のでテスト目的的は OK
        result = preblock_hook._is_in_cc_memory_project()
        assert result is False

    def test_pyproject_found_in_parent(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "cc-memory"\n'
        )
        subdir = tmp_path / "src" / "deep" / "path"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        assert preblock_hook._is_in_cc_memory_project() is True


# ---------------------------------------------------------------------------
# main フロー (block 判定 + log 記録)
# ---------------------------------------------------------------------------


def _run_main_with_event(event: dict, capsys) -> dict:
    """stdin に event を流して main() を呼び、stdout 出力を dict として返す。"""
    sys.stdin = io.StringIO(json.dumps(event))
    try:
        preblock_hook.main()
    finally:
        sys.stdin = sys.__stdin__
    captured = capsys.readouterr()
    text = captured.out.strip()
    if not text or text == "{}":
        return {}
    return json.loads(text)


@pytest.fixture
def cc_memory_cwd(tmp_path, monkeypatch):
    """cc-memory project 内っぽい cwd を用意する fixture。"""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "cc-memory"\n'
    )
    monkeypatch.chdir(tmp_path)
    # opt-out 環境変数は明示的に消しておく
    monkeypatch.delenv("CC_MEMORY_LEAK_GUARD", raising=False)
    # log path を tmp 配下に逃がして home 汚染を防ぐ
    log_path = tmp_path / "log" / "preblock_hook.jsonl"
    monkeypatch.setattr(preblock_hook, "LOG_PATH", log_path)
    return tmp_path, log_path


class TestMainBlockFlow:
    def test_empty_stdin_passes_through(self, capsys, cc_memory_cwd):
        sys.stdin = io.StringIO("")
        try:
            preblock_hook.main()
        finally:
            sys.stdin = sys.__stdin__
        assert capsys.readouterr().out.strip() == "{}"

    def test_clean_input_passes_through(self, capsys, cc_memory_cwd):
        out = _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
                "session_id": "s1",
            },
            capsys,
        )
        assert out == {}

    def test_code_literal_in_bash_blocks(self, capsys, cc_memory_cwd):
        out = _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo M#123 hello"},
                "session_id": "s1",
            },
            capsys,
        )
        spec = out["hookSpecificOutput"]
        assert spec["hookEventName"] == "PreToolUse"
        assert spec["permissionDecision"] == "deny"
        assert "M#123" in spec["permissionDecisionReason"]

    def test_fullword_literal_in_write_blocks(self, capsys, cc_memory_cwd):
        out = _run_main_with_event(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/tmp/x.md",
                    "content": "see log #45 in docs",
                },
                "session_id": "s1",
            },
            capsys,
        )
        spec = out["hookSpecificOutput"]
        assert spec["permissionDecision"] == "deny"
        assert "log #45" in spec["permissionDecisionReason"]

    def test_escape_not_blocked(self, capsys, cc_memory_cwd):
        out = _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo \\M#123 literal"},
                "session_id": "s1",
            },
            capsys,
        )
        assert out == {}

    def test_allowlist_tool_passes_through(self, capsys, cc_memory_cwd):
        # cc-memory MCP は scan されない
        out = _run_main_with_event(
            {
                "tool_name": "mcp__plugin_claude-code-memory_cc-memory__search",
                "tool_input": {"keyword": "D#123"},
                "session_id": "s1",
            },
            capsys,
        )
        assert out == {}

    def test_allowlist_read_passes_through(self, capsys, cc_memory_cwd):
        out = _run_main_with_event(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "/tmp/M#1.md"},
                "session_id": "s1",
            },
            capsys,
        )
        assert out == {}

    def test_allowlist_skips_project_check_io(
        self, capsys, cc_memory_cwd, monkeypatch
    ):
        # allowlist tool では _is_in_cc_memory_project が呼ばれないこと
        # (頻出 tool で pyproject.toml の読み直しを避ける最適化)
        call_count = {"n": 0}

        def _spy() -> bool:
            call_count["n"] += 1
            return True

        monkeypatch.setattr(preblock_hook, "_is_in_cc_memory_project", _spy)
        _run_main_with_event(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "/tmp/x.md"},
                "session_id": "s1",
            },
            capsys,
        )
        assert call_count["n"] == 0

    def test_opt_out_env_var_passes_through(
        self, capsys, cc_memory_cwd, monkeypatch
    ):
        monkeypatch.setenv("CC_MEMORY_LEAK_GUARD", "off")
        out = _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo M#1 leak"},
                "session_id": "s1",
            },
            capsys,
        )
        assert out == {}

    def test_opt_out_case_insensitive(self, capsys, cc_memory_cwd, monkeypatch):
        monkeypatch.setenv("CC_MEMORY_LEAK_GUARD", "OFF")
        out = _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo M#1 leak"},
                "session_id": "s1",
            },
            capsys,
        )
        assert out == {}

    def test_opt_out_other_values_do_not_skip(
        self, capsys, cc_memory_cwd, monkeypatch
    ):
        # "on" / "1" / "true" などは opt-out ではない
        monkeypatch.setenv("CC_MEMORY_LEAK_GUARD", "on")
        out = _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo M#1 leak"},
                "session_id": "s1",
            },
            capsys,
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_non_cc_memory_project_passes_through(
        self, capsys, tmp_path, monkeypatch
    ):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "other-project"\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CC_MEMORY_LEAK_GUARD", raising=False)
        out = _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo M#1 leak"},
                "session_id": "s1",
            },
            capsys,
        )
        assert out == {}


class TestLogEventFields:
    def test_log_written_with_required_fields(self, capsys, cc_memory_cwd):
        _, log_path = cc_memory_cwd
        _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo M#123 and log #45"},
                "session_id": "session-xyz",
            },
            capsys,
        )
        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["tool_name"] == "Bash"
        assert rec["decision"] == "block"
        assert "M#123" in rec["matches"]
        assert "log #45" in rec["matches"]
        assert rec["tool_input_field"] == ["command"]
        assert rec["cwd"] == str(cc_memory_cwd[0])
        assert rec["session_id"] == "session-xyz"
        assert "timestamp" in rec

    def test_log_field_for_nested_dict(self, capsys, cc_memory_cwd):
        _, log_path = cc_memory_cwd
        _run_main_with_event(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/tmp/x.md",
                    "content": "see log #1 here",
                },
                "session_id": "s1",
            },
            capsys,
        )
        rec = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert "content" in rec["tool_input_field"]

    def test_no_log_when_clean(self, capsys, cc_memory_cwd):
        _, log_path = cc_memory_cwd
        _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
                "session_id": "s1",
            },
            capsys,
        )
        assert not log_path.exists()

    def test_log_write_failure_is_silent(
        self, capsys, cc_memory_cwd, monkeypatch
    ):
        # LOG_PATH の親に書き込み権限がない状況を疑似的に再現するため、
        # _log_event 内の open を強制的に失敗させる
        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(preblock_hook.pathlib.Path, "mkdir", boom)

        out = _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo M#1"},
                "session_id": "s1",
            },
            capsys,
        )
        # block 動作は継続している
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
