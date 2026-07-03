"""scripts/gate_check.py の検出器ロジックを検証するunit test。

大半は git を介さない純粋関数レベルのテスト(FileChange/DiffLine/NumstatRow を
直接組み立てて検出器に渡す)。git の実挙動に依存する項目(自己保護パスの実ファイル
接触、判定不能フォールバック、決定性、公開IFがdiffに無いときAST未実行)のみ、
一時git リポジトリを使った統合テストとして書く。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.gate_check import (  # noqa: E402
    DETECTOR_SELF_PATHS,
    MAX_FILES,
    MAX_LINES,
    PUBLIC_IF_PATHS,
    AxisB,
    DiffLine,
    FileChange,
    Finding,
    NumstatRow,
    classify,
    compute_axis_b,
    compute_has_tests,
    compute_mechanical_rollback,
    compute_public_if_delta,
    count_diff_size,
    detect_binary_change,
    detect_data_destructive,
    detect_ddl_in_code,
    detect_dependency_change,
    detect_migration_touch,
    detect_public_if,
    extract_tool_surface,
    is_self_touched,
    parse_diff_lines,
    parse_name_status,
    parse_numstat,
    render_markdown,
    run_detector,
    verdict_to_json,
)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _fc(path: str, status: str, old_path: str | None = None, is_binary: bool = False) -> FileChange:
    return FileChange(path=path, old_path=old_path, status=status, additions=0, deletions=0, is_binary=is_binary)


def _dl(path: str, sign: str, text: str, lineno: int = 1) -> DiffLine:
    if sign == "+":
        return DiffLine(path=path, sign="+", new_lineno=lineno, old_lineno=None, text=text)
    return DiffLine(path=path, sign="-", new_lineno=None, old_lineno=lineno, text=text)


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True)


def _commit_all(path: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", message], check=True)
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# migration_touch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["A", "M", "D"])
def test_migration_touch_hits_for_add_modify_delete(status):
    changes = [_fc("migrations/0049_x.sql", status)]
    findings = detect_migration_touch(changes)
    assert len(findings) == 1
    assert findings[0].status == "counted"
    assert findings[0].detector == "migration_touch"


def test_migration_touch_hits_on_rename_into_migrations():
    changes = [_fc("migrations/0050_y.sql", "R", old_path="scripts/not_migration.sql")]
    findings = detect_migration_touch(changes)
    assert len(findings) == 1


def test_migration_touch_hits_on_rename_out_of_migrations():
    changes = [_fc("scripts/not_migration.sql", "R", old_path="migrations/0049_x.sql")]
    findings = detect_migration_touch(changes)
    assert len(findings) == 1


def test_migration_touch_no_hit_for_non_migration_path():
    changes = [_fc("src/services/foo.py", "M")]
    assert detect_migration_touch(changes) == []


# ---------------------------------------------------------------------------
# ddl_in_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "conn.execute('CREATE TABLE foo (id INTEGER)')",
        "conn.execute('ALTER TABLE foo DROP COLUMN bar')",
        "conn.execute('DROP INDEX idx_foo')",
        "conn.execute('CREATE VIRTUAL TABLE foo USING fts5(x)')",
        "conn.execute('PRAGMA foreign_keys = ON')",
    ],
)
def test_ddl_in_code_hits_added_line(text):
    findings = detect_ddl_in_code([_dl("src/services/foo.py", "+", text)])
    assert len(findings) == 1
    assert findings[0].status == "counted"


def test_ddl_in_code_hits_removed_line():
    findings = detect_ddl_in_code([_dl("src/services/foo.py", "-", "conn.execute('CREATE TABLE foo (id INTEGER)')")])
    assert len(findings) == 1
    assert findings[0].lineno == 1


def test_ddl_in_code_pragma_read_does_not_hit():
    findings = detect_ddl_in_code([_dl("src/services/foo.py", "+", "conn.execute('PRAGMA table_info(foo)')")])
    assert findings == []


def test_ddl_in_code_tests_dir_is_downgraded():
    findings = detect_ddl_in_code([_dl("tests/unit/test_foo.py", "+", "conn.execute('CREATE TABLE foo (id INTEGER)')")])
    assert len(findings) == 1
    assert findings[0].status == "downgraded_tests"
    # downgraded findings はゲーティングに数えない
    assert not any(f.status == "counted" for f in findings)


@pytest.mark.parametrize("path", ["docs/spec/db-schema.md", "README.md"])
def test_ddl_in_code_docs_and_md_are_excluded_entirely(path):
    findings = detect_ddl_in_code([_dl(path, "+", "CREATE TABLE foo (id INTEGER)")])
    assert findings == []


def test_ddl_in_code_comment_false_positive_is_locked_in():
    """コメント内のDDL語は誤検出を許容する(安全側に倒れるため仕様として固定)。"""
    findings = detect_ddl_in_code([_dl("src/services/foo.py", "+", "# ALTER TABLE のメモ")])
    assert len(findings) == 1
    assert findings[0].status == "counted"


# ---------------------------------------------------------------------------
# data_destructive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "conn.execute('DELETE FROM foo WHERE id = ?')",
        "conn.execute('UPDATE foo SET bar = ?')",
        "shutil.rmtree(path)",
        "os.remove(path)",
    ],
)
def test_data_destructive_hits_added_line(text):
    findings = detect_data_destructive([_dl("src/services/foo.py", "+", text)])
    assert len(findings) == 1
    assert findings[0].status == "counted"


def test_data_destructive_does_not_hit_function_name_update_material():
    findings = detect_data_destructive([_dl("src/main.py", "+", "update_material(title, content)")])
    assert findings == []


def test_data_destructive_ignores_removed_lines():
    findings = detect_data_destructive([_dl("src/services/foo.py", "-", "conn.execute('DELETE FROM foo')")])
    assert findings == []


def test_data_destructive_tests_dir_is_downgraded():
    findings = detect_data_destructive([_dl("tests/unit/test_foo.py", "+", "conn.execute('DELETE FROM foo')")])
    assert len(findings) == 1
    assert findings[0].status == "downgraded_tests"


# ---------------------------------------------------------------------------
# public_if
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", PUBLIC_IF_PATHS)
def test_public_if_hits_for_every_listed_path(path):
    findings = detect_public_if([_fc(path, "M")])
    assert len(findings) == 1
    assert findings[0].detector == "public_if"


def test_public_if_no_hit_for_unrelated_src_path():
    findings = detect_public_if([_fc("src/services/foo.py", "M")])
    assert findings == []


def test_public_if_hits_regardless_of_which_lines_changed():
    # public_if はパス一致のみで判定する。diff_lines の中身は問わない。
    findings = detect_public_if([_fc("src/main.py", "M")])
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# binary_change / dependency_change
# ---------------------------------------------------------------------------


def test_binary_change_hits_for_binary_file():
    changes = [_fc("assets/logo.png", "A", is_binary=True), _fc("src/foo.py", "M", is_binary=False)]
    findings = detect_binary_change(changes)
    assert len(findings) == 1
    assert findings[0].path == "assets/logo.png"
    assert findings[0].status == "counted"


def test_dependency_change_marks_policy_pending():
    findings = detect_dependency_change([_fc("pyproject.toml", "M")])
    assert len(findings) == 1
    assert findings[0].status == "policy_pending"


def test_dependency_change_hits_uv_lock_too():
    findings = detect_dependency_change([_fc("uv.lock", "M")])
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# self_touched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", DETECTOR_SELF_PATHS)
def test_is_self_touched_true_for_each_self_path(path):
    assert is_self_touched([_fc(path, "M")]) is True


def test_is_self_touched_false_when_untouched():
    assert is_self_touched([_fc("src/services/foo.py", "M")]) is False


# ---------------------------------------------------------------------------
# classify (短絡順序)
# ---------------------------------------------------------------------------


_MET_AXIS_B = AxisB(lines_changed=1, files_changed=1, size_ok=True, has_tests=True, mechanical_rollback=True, met=True)
_UNMET_AXIS_B = AxisB(lines_changed=9999, files_changed=99, size_ok=False, has_tests=True, mechanical_rollback=True, met=False)


def test_classify_errors_wins_over_everything():
    counted_finding = Finding(detector="migration_touch", path="migrations/x.sql", lineno=None, evidence="", status="counted")
    classification, reason = classify([counted_finding], _MET_AXIS_B, errors=["boom"], self_touched=True)
    assert classification == "pre_go"
    assert reason == "detector_error"


def test_classify_self_touched_without_errors():
    classification, reason = classify([], _MET_AXIS_B, errors=[], self_touched=True)
    assert (classification, reason) == ("pre_go", "self_protection")


def test_classify_axis_a_hit():
    counted_finding = Finding(detector="migration_touch", path="migrations/x.sql", lineno=None, evidence="", status="counted")
    classification, reason = classify([counted_finding], _MET_AXIS_B, errors=[], self_touched=False)
    assert (classification, reason) == ("pre_go", "axis_a_hit")


def test_classify_policy_pending():
    pending_finding = Finding(detector="dependency_change", path="pyproject.toml", lineno=None, evidence="", status="policy_pending")
    classification, reason = classify([pending_finding], _MET_AXIS_B, errors=[], self_touched=False)
    assert (classification, reason) == ("gray", "policy_pending")
    # 軸B充足でも policy_pending が優先され post_veto_candidate にはならない


def test_classify_axis_b_met():
    classification, reason = classify([], _MET_AXIS_B, errors=[], self_touched=False)
    assert (classification, reason) == ("post_veto_candidate", "axis_b_met")


def test_classify_axis_b_unmet():
    classification, reason = classify([], _UNMET_AXIS_B, errors=[], self_touched=False)
    assert (classification, reason) == ("gray", "axis_b_unmet")


# ---------------------------------------------------------------------------
# axis B
# ---------------------------------------------------------------------------


def test_count_diff_size_excludes_uv_lock_from_both_lines_and_files():
    rows = [
        NumstatRow(path="uv.lock", old_path=None, additions=3000, deletions=3000, is_binary=False),
        NumstatRow(path="src/foo.py", old_path=None, additions=10, deletions=5, is_binary=False),
    ]
    lines, files = count_diff_size(rows)
    assert lines == 15
    assert files == 1


def test_count_diff_size_excludes_tests_docs_md_from_lines_but_counts_files():
    rows = [
        NumstatRow(path="tests/unit/test_foo.py", old_path=None, additions=100, deletions=0, is_binary=False),
        NumstatRow(path="docs/spec/foo.md", old_path=None, additions=50, deletions=0, is_binary=False),
        NumstatRow(path="README.md", old_path=None, additions=20, deletions=0, is_binary=False),
        NumstatRow(path="src/foo.py", old_path=None, additions=10, deletions=5, is_binary=False),
    ]
    lines, files = count_diff_size(rows)
    assert lines == 15  # src/foo.py のみ
    assert files == 4


def test_compute_has_tests_true_when_tests_present():
    rows = [NumstatRow(path="tests/unit/test_foo.py", old_path=None, additions=1, deletions=0, is_binary=False)]
    assert compute_has_tests(rows) is True


def test_compute_has_tests_false_when_no_tests_and_not_docs_only():
    rows = [NumstatRow(path="src/foo.py", old_path=None, additions=1, deletions=0, is_binary=False)]
    assert compute_has_tests(rows) is False


def test_compute_has_tests_waived_when_all_docs_only():
    rows = [
        NumstatRow(path="docs/spec/foo.md", old_path=None, additions=1, deletions=0, is_binary=False),
        NumstatRow(path="README.md", old_path=None, additions=1, deletions=0, is_binary=False),
    ]
    assert compute_has_tests(rows) == "waived_docs_only"


def test_compute_mechanical_rollback_false_on_migration_touch():
    findings = [Finding(detector="migration_touch", path="migrations/x.sql", lineno=None, evidence="", status="counted")]
    assert compute_mechanical_rollback(findings) is False


def test_compute_mechanical_rollback_true_when_only_downgraded_findings():
    findings = [Finding(detector="ddl_in_code", path="tests/unit/test_foo.py", lineno=1, evidence="", status="downgraded_tests")]
    assert compute_mechanical_rollback(findings) is True


def test_compute_mechanical_rollback_true_when_only_public_if_hit():
    findings = [Finding(detector="public_if", path="src/main.py", lineno=None, evidence="", status="counted")]
    assert compute_mechanical_rollback(findings) is True


def test_compute_axis_b_size_exceeded_is_unmet():
    rows = [NumstatRow(path="src/foo.py", old_path=None, additions=MAX_LINES + 1, deletions=0, is_binary=False),
            NumstatRow(path="tests/unit/test_foo.py", old_path=None, additions=1, deletions=0, is_binary=False)]
    axis_b = compute_axis_b(rows, [])
    assert axis_b.size_ok is False
    assert axis_b.met is False


def test_compute_axis_b_missing_tests_is_unmet():
    rows = [NumstatRow(path="src/foo.py", old_path=None, additions=10, deletions=0, is_binary=False)]
    axis_b = compute_axis_b(rows, [])
    assert axis_b.has_tests is False
    assert axis_b.met is False


def test_compute_axis_b_too_many_files_is_unmet():
    rows = [NumstatRow(path=f"src/foo_{i}.py", old_path=None, additions=1, deletions=0, is_binary=False) for i in range(MAX_FILES + 1)]
    rows.append(NumstatRow(path="tests/unit/test_foo.py", old_path=None, additions=1, deletions=0, is_binary=False))
    axis_b = compute_axis_b(rows, [])
    assert axis_b.size_ok is False
    assert axis_b.met is False


# ---------------------------------------------------------------------------
# parse_name_status / parse_numstat / parse_diff_lines(git出力形式の直接パース)
# ---------------------------------------------------------------------------


def test_parse_name_status_handles_add_modify_delete_rename():
    raw = b"A\0new_file.py\0M\0src/foo.py\0D\0old_file.py\0R100\0scripts/old.py\0scripts/new.py\0"
    changes = parse_name_status(raw)
    by_path = {c.path: c for c in changes}
    assert by_path["new_file.py"].status == "A"
    assert by_path["src/foo.py"].status == "M"
    assert by_path["old_file.py"].status == "D"
    assert by_path["scripts/new.py"].status == "R"
    assert by_path["scripts/new.py"].old_path == "scripts/old.py"


def test_parse_numstat_binary_row():
    rows = parse_numstat("-\t-\tassets/logo.png\n")
    assert len(rows) == 1
    assert rows[0].is_binary is True
    assert rows[0].additions == -1
    assert rows[0].deletions == -1


def test_parse_numstat_rename_with_common_prefix():
    rows = parse_numstat("1\t0\tscripts/{snapshot.py => snapshot_renamed.py}\n")
    assert rows[0].path == "scripts/snapshot_renamed.py"
    assert rows[0].old_path == "scripts/snapshot.py"


def test_parse_diff_lines_tracks_added_and_removed_line_numbers():
    diff_text = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "index 111..222 100644\n"
        "--- a/src/foo.py\n"
        "+++ b/src/foo.py\n"
        "@@ -10,2 +10,2 @@\n"
        "-old line one\n"
        "-old line two\n"
        "+new line one\n"
        "+new line two\n"
    )
    lines = parse_diff_lines(diff_text)
    added = [dl for dl in lines if dl.sign == "+"]
    removed = [dl for dl in lines if dl.sign == "-"]
    assert [dl.new_lineno for dl in added] == [10, 11]
    assert [dl.old_lineno for dl in removed] == [10, 11]
    assert added[0].text == "new line one"


# ---------------------------------------------------------------------------
# AST 抽出(public_if_delta)
# ---------------------------------------------------------------------------


_BASE_SRC_TWO_TOOLS = '''
@mcp.tool()
def add_topic(title: str) -> dict:
    """add topic"""
    return {}

@mcp.tool()
def remove_topic(id: int) -> dict:
    """remove topic"""
    return {}
'''


def test_extract_tool_surface_only_picks_mcp_tool_decorated_functions():
    errors: list[str] = []
    tools = extract_tool_surface(_BASE_SRC_TWO_TOOLS, errors, "test")
    assert set(tools) == {"add_topic", "remove_topic"}
    assert errors == []


def test_public_if_delta_detects_tool_added():
    base = "@mcp.tool()\ndef remove_topic(id: int) -> dict:\n    \"\"\"remove\"\"\"\n    return {}\n"
    head = _BASE_SRC_TWO_TOOLS
    errors: list[str] = []
    delta = compute_public_if_delta(base, head, errors)
    assert delta["tools_added"] == ["add_topic"]
    assert delta["tools_removed"] == []


def test_public_if_delta_detects_tool_removed():
    base = _BASE_SRC_TWO_TOOLS
    head = "@mcp.tool()\ndef remove_topic(id: int) -> dict:\n    \"\"\"remove\"\"\"\n    return {}\n"
    errors: list[str] = []
    delta = compute_public_if_delta(base, head, errors)
    assert delta["tools_removed"] == ["add_topic"]
    assert delta["tools_added"] == []


def test_public_if_delta_detects_param_added():
    base = "@mcp.tool()\ndef add_topic(title: str) -> dict:\n    \"\"\"doc\"\"\"\n    return {}\n"
    head = "@mcp.tool()\ndef add_topic(title: str, tags: list[str] = None) -> dict:\n    \"\"\"doc\"\"\"\n    return {}\n"
    errors: list[str] = []
    delta = compute_public_if_delta(base, head, errors)
    assert len(delta["params_changed"]) == 1
    assert delta["params_changed"][0].startswith("add_topic:")
    assert delta["docstring_changed"] == []


def test_public_if_delta_detects_default_value_changed():
    base = "@mcp.tool()\ndef foo(x: int = 1) -> dict:\n    \"\"\"doc\"\"\"\n    return {}\n"
    head = "@mcp.tool()\ndef foo(x: int = 2) -> dict:\n    \"\"\"doc\"\"\"\n    return {}\n"
    errors: list[str] = []
    delta = compute_public_if_delta(base, head, errors)
    assert len(delta["params_changed"]) == 1


def test_public_if_delta_detects_docstring_changed():
    base = "@mcp.tool()\ndef foo(x: int) -> dict:\n    \"\"\"old doc\"\"\"\n    return {}\n"
    head = "@mcp.tool()\ndef foo(x: int) -> dict:\n    \"\"\"new doc\"\"\"\n    return {}\n"
    errors: list[str] = []
    delta = compute_public_if_delta(base, head, errors)
    assert delta["docstring_changed"] == ["foo"]
    assert delta["params_changed"] == []


def test_public_if_delta_records_parse_failure_without_raising():
    base = _BASE_SRC_TWO_TOOLS
    head = "def broken(:\n    this is not python\n"
    errors: list[str] = []
    delta = compute_public_if_delta(base, head, errors)
    assert delta["tools_removed"] == ["add_topic", "remove_topic"]  # head 側は parse失敗で空扱い
    assert any("ast_parse_failed:head" in e for e in errors)


# ---------------------------------------------------------------------------
# 統合テスト(実 git リポジトリを使う)
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    return repo


def test_run_detector_returns_pre_go_detector_error_when_git_fails(tmp_path: Path):
    missing_repo = tmp_path / "does-not-exist"
    verdict = run_detector(missing_repo, "main", "HEAD")
    assert verdict["classification"] == "pre_go"
    assert verdict["reason"] == "detector_error"
    assert verdict["errors"]


def test_run_detector_self_protection_via_real_file_touch(git_repo: Path):
    (git_repo / "scripts").mkdir()
    (git_repo / "scripts" / "gate_check.py").write_text("# placeholder\n")
    base_sha = _commit_all(git_repo, "base")
    (git_repo / "scripts" / "gate_check.py").write_text("# placeholder changed\n")
    head_sha = _commit_all(git_repo, "touch detector")

    verdict = run_detector(git_repo, base_sha, head_sha)
    assert verdict["classification"] == "pre_go"
    assert verdict["reason"] == "self_protection"


def test_run_detector_ast_not_run_when_main_py_untouched(git_repo: Path):
    (git_repo / "src").mkdir()
    (git_repo / "src" / "foo.py").write_text("x = 1\n")
    base_sha = _commit_all(git_repo, "base")
    (git_repo / "src" / "foo.py").write_text("x = 2\n")
    head_sha = _commit_all(git_repo, "change foo")

    verdict = run_detector(git_repo, base_sha, head_sha)
    assert verdict["public_if_delta"] == {
        "tools_added": [],
        "tools_removed": [],
        "params_changed": [],
        "docstring_changed": [],
    }
    assert not any("ast_parse_failed" in e for e in verdict["errors"])


def test_run_detector_is_deterministic_across_runs(git_repo: Path):
    (git_repo / "migrations").mkdir()
    (git_repo / "migrations" / "0001_init.sql").write_text("CREATE TABLE foo (id INTEGER);\n")
    (git_repo / "src").mkdir()
    (git_repo / "src").joinpath("main.py").write_text("@mcp.tool()\ndef foo():\n    pass\n")
    base_sha = _commit_all(git_repo, "base")
    (git_repo / "migrations" / "0002_next.sql").write_text("ALTER TABLE foo ADD COLUMN bar TEXT;\n")
    head_sha = _commit_all(git_repo, "add migration")

    v1 = run_detector(git_repo, base_sha, head_sha)
    v2 = run_detector(git_repo, base_sha, head_sha)
    assert verdict_to_json(v1) == verdict_to_json(v2)
    assert v1["classification"] == "pre_go"
    assert v1["reason"] == "axis_a_hit"


def test_run_detector_findings_are_sorted_by_detector_then_path(git_repo: Path):
    (git_repo / "migrations").mkdir()
    (git_repo / "migrations" / "0001_init.sql").write_text("CREATE TABLE foo (id INTEGER);\n")
    (git_repo / "src").mkdir()
    (git_repo / "src" / "main.py").write_text("x = 1\n")
    base_sha = _commit_all(git_repo, "base")
    (git_repo / "migrations" / "0002_a.sql").write_text("CREATE TABLE bar (id INTEGER);\n")
    (git_repo / "src" / "main.py").write_text("x = 2\n")
    head_sha = _commit_all(git_repo, "touch multiple detectors")

    verdict = run_detector(git_repo, base_sha, head_sha)
    findings = verdict["axis_a"]["findings"]
    detectors = [f["detector"] for f in findings]
    assert detectors == sorted(detectors)


def test_render_markdown_produces_expected_sections():
    verdict = {
        "classification": "pre_go",
        "reason": "axis_a_hit",
        "axis_a": {"findings": [{"detector": "migration_touch", "path": "migrations/x.sql", "lineno": None, "evidence": "status=A", "status": "counted"}]},
        "axis_b": {"lines_changed": 10, "files_changed": 2, "has_tests": True, "mechanical_rollback": True},
        "public_if_delta": {"tools_added": [], "tools_removed": [], "params_changed": [], "docstring_changed": []},
    }
    md = render_markdown(verdict)
    assert "ブラスト半径" in md
    assert "revert容易性" in md
    assert "migration_touch" in md


def test_run_detector_end_to_end_smoke_matches_classify(git_repo: Path):
    (git_repo / "src").mkdir()
    (git_repo / "src" / "foo.py").write_text("x = 1\n")
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_foo.py").write_text("def test_x(): assert True\n")
    base_sha = _commit_all(git_repo, "base")
    (git_repo / "src" / "foo.py").write_text("x = 2\n")
    (git_repo / "tests" / "test_foo.py").write_text("def test_x(): assert True\n# updated\n")
    head_sha = _commit_all(git_repo, "small clean change with tests")

    verdict = run_detector(git_repo, base_sha, head_sha)
    assert verdict["classification"] == "post_veto_candidate"
    assert verdict["reason"] == "axis_b_met"
    # JSON化してもクラッシュしない
    json.loads(verdict_to_json(verdict))
