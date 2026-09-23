"""scripts/lint_doc_cochange.py のユニットテスト。

判定ロジック本体（純粋関数）は git subprocess を挟まず直接テストする。
main() の配線テストのみ、git_* 関数を monkeypatch して外部境界（subprocess）を切り離す。
"""
import scripts.lint_doc_cochange as lint_doc_cochange
from scripts.lint_doc_cochange import (
    DB_SCHEMA_DOC,
    MCP_TOOLS_DOC,
    check_readme_tables,
    diff_tool_signatures,
    evaluate,
    extract_readme_skill_names,
    extract_readme_tool_names,
    extract_skill_dir_names,
    extract_tool_signatures,
    has_exception_marker,
)

BASE_MAIN_PY = '''
from fastmcp import FastMCP

mcp = FastMCP("cc-memory")


@mcp.tool()
def add_topic(title: str, description: str, tags: list[str]) -> dict:
    """トピック追加。"""
    return topic_service.add_topic(title, description, tags)


@mcp.tool()
def get_topics(limit: int = 10) -> dict:
    """トピック一覧取得。"""
    return topic_service.get_topics(limit)


def _internal_helper(x: int) -> int:
    return x + 1
'''

HEAD_MAIN_PY_ADDED_TOOL = BASE_MAIN_PY + '''

@mcp.tool()
def export_material(material_id: int, dest_path: str | None = None) -> dict:
    """資材をmdとしてexportする。"""
    return material_service.export_material(material_id, dest_path)
'''

HEAD_MAIN_PY_CHANGED_SIGNATURE = BASE_MAIN_PY.replace(
    "def get_topics(limit: int = 10) -> dict:",
    "def get_topics(limit: int = 10, offset: int = 0) -> dict:",
)

HEAD_MAIN_PY_DOCSTRING_ONLY = BASE_MAIN_PY.replace(
    '"""トピック一覧取得。"""', '"""トピック一覧を取得する（新しい説明文）。"""'
)

HEAD_MAIN_PY_INVALID_SYNTAX = BASE_MAIN_PY + "\ndef broken(:\n"


# --- extract_tool_signatures ---


def test_extract_tool_signatures_finds_only_mcp_tool_functions():
    sigs = extract_tool_signatures(BASE_MAIN_PY)
    assert sigs is not None
    assert set(sigs) == {"add_topic", "get_topics"}


def test_extract_tool_signatures_returns_none_on_syntax_error():
    assert extract_tool_signatures(HEAD_MAIN_PY_INVALID_SYNTAX) is None


def test_extract_tool_signatures_captures_arg_shape():
    sigs = extract_tool_signatures(BASE_MAIN_PY)
    assert sigs["get_topics"] == [("limit", "int", True)]
    assert sigs["add_topic"] == [
        ("title", "str", False),
        ("description", "str", False),
        ("tags", "list[str]", False),
    ]


# --- diff_tool_signatures ---


def test_diff_tool_signatures_detects_added_tool():
    base = extract_tool_signatures(BASE_MAIN_PY)
    head = extract_tool_signatures(HEAD_MAIN_PY_ADDED_TOOL)
    diff = diff_tool_signatures(base, head)
    assert diff.get("added") == ["export_material"]
    assert "removed" not in diff
    assert "changed" not in diff


def test_diff_tool_signatures_detects_changed_arg():
    base = extract_tool_signatures(BASE_MAIN_PY)
    head = extract_tool_signatures(HEAD_MAIN_PY_CHANGED_SIGNATURE)
    diff = diff_tool_signatures(base, head)
    assert diff.get("changed") == ["get_topics"]


def test_diff_tool_signatures_ignores_docstring_only_change():
    base = extract_tool_signatures(BASE_MAIN_PY)
    head = extract_tool_signatures(HEAD_MAIN_PY_DOCSTRING_ONLY)
    assert diff_tool_signatures(base, head) == {}


def test_diff_tool_signatures_detects_removed_tool():
    base = extract_tool_signatures(BASE_MAIN_PY)
    head = extract_tool_signatures(BASE_MAIN_PY.replace(
        '@mcp.tool()\ndef get_topics(limit: int = 10) -> dict:\n    """トピック一覧取得。"""\n    return topic_service.get_topics(limit)\n\n\n',
        "",
    ))
    diff = diff_tool_signatures(base, head)
    assert diff.get("removed") == ["get_topics"]


# --- has_exception_marker ---


def test_has_exception_marker_matches_commit_message():
    assert has_exception_marker("[no-schema-shape-change]", "fix: xxx\n\n[no-schema-shape-change]", "")


def test_has_exception_marker_matches_pr_body():
    assert has_exception_marker("[no-schema-shape-change]", "", "PR body ... [no-schema-shape-change]")


def test_has_exception_marker_false_when_absent():
    assert not has_exception_marker("[no-schema-shape-change]", "fix: xxx", "PR body")


# --- evaluate: migrations <-> db-schema.md ---


def test_evaluate_fails_when_migration_changed_without_schema_doc():
    failures, warnings = evaluate(
        changed_files=["migrations/0050_add_x.sql"],
        commit_messages="fix: add column",
        pr_body="",
        base_main_py=None,
        head_main_py=None,
    )
    assert warnings == []
    assert len(failures) == 1
    assert DB_SCHEMA_DOC in failures[0]


def test_evaluate_passes_when_migration_and_schema_doc_both_changed():
    failures, _ = evaluate(
        changed_files=["migrations/0050_add_x.sql", DB_SCHEMA_DOC],
        commit_messages="",
        pr_body="",
        base_main_py=None,
        head_main_py=None,
    )
    assert failures == []


def test_evaluate_passes_with_exception_marker_in_commit_message():
    failures, _ = evaluate(
        changed_files=["migrations/0050_add_x.sql"],
        commit_messages="chore: index追加\n\n[no-schema-shape-change]",
        pr_body="",
        base_main_py=None,
        head_main_py=None,
    )
    assert failures == []


def test_evaluate_passes_with_exception_marker_in_pr_body():
    failures, _ = evaluate(
        changed_files=["migrations/0050_add_x.sql"],
        commit_messages="",
        pr_body="## 概要\n...\n[no-schema-shape-change]",
        base_main_py=None,
        head_main_py=None,
    )
    assert failures == []


def test_evaluate_ignores_non_sql_migrations_dir_changes():
    failures, _ = evaluate(
        changed_files=["migrations/README.md"],
        commit_messages="",
        pr_body="",
        base_main_py=None,
        head_main_py=None,
    )
    assert failures == []


# --- evaluate: main.py tool surface <-> mcp-tools.md ---


def test_evaluate_fails_when_tool_added_without_doc_update():
    failures, warnings = evaluate(
        changed_files=["src/main.py"],
        commit_messages="",
        pr_body="",
        base_main_py=BASE_MAIN_PY,
        head_main_py=HEAD_MAIN_PY_ADDED_TOOL,
    )
    assert warnings == []
    assert len(failures) == 1
    assert MCP_TOOLS_DOC in failures[0]


def test_evaluate_passes_when_tool_added_with_doc_update():
    failures, _ = evaluate(
        changed_files=["src/main.py", MCP_TOOLS_DOC],
        commit_messages="",
        pr_body="",
        base_main_py=BASE_MAIN_PY,
        head_main_py=HEAD_MAIN_PY_ADDED_TOOL,
    )
    assert failures == []


def test_evaluate_passes_with_no_tool_surface_change_marker():
    failures, _ = evaluate(
        changed_files=["src/main.py"],
        commit_messages="feat: xxx\n\n[no-tool-surface-change]",
        pr_body="",
        base_main_py=BASE_MAIN_PY,
        head_main_py=HEAD_MAIN_PY_ADDED_TOOL,
    )
    assert failures == []


def test_evaluate_passes_when_main_py_changed_but_no_signature_diff():
    failures, _ = evaluate(
        changed_files=["src/main.py"],
        commit_messages="",
        pr_body="",
        base_main_py=BASE_MAIN_PY,
        head_main_py=HEAD_MAIN_PY_DOCSTRING_ONLY,
    )
    assert failures == []


def test_evaluate_warns_only_on_syntax_error_in_head():
    failures, warnings = evaluate(
        changed_files=["src/main.py"],
        commit_messages="",
        pr_body="",
        base_main_py=BASE_MAIN_PY,
        head_main_py=HEAD_MAIN_PY_INVALID_SYNTAX,
    )
    assert failures == []
    assert len(warnings) == 1


def test_evaluate_warns_only_when_source_unavailable():
    failures, warnings = evaluate(
        changed_files=["src/main.py"],
        commit_messages="",
        pr_body="",
        base_main_py=None,
        head_main_py=None,
    )
    assert failures == []
    assert len(warnings) == 1


def test_evaluate_skips_tool_surface_check_when_main_py_not_changed():
    failures, warnings = evaluate(
        changed_files=["docs/spec/mcp-tools.md"],
        commit_messages="",
        pr_body="",
        base_main_py=None,
        head_main_py=None,
    )
    assert failures == []
    assert warnings == []


def test_evaluate_reports_both_failures_independently():
    failures, _ = evaluate(
        changed_files=["migrations/0050_add_x.sql", "src/main.py"],
        commit_messages="",
        pr_body="",
        base_main_py=BASE_MAIN_PY,
        head_main_py=HEAD_MAIN_PY_ADDED_TOOL,
    )
    assert len(failures) == 2


# --- README表パース ---

README_TEXT = """# CALM

## MCPツール

| カテゴリ | ツール | 説明 |
|---------|--------|------|
| トピック | `add_topic`, `get_topics` | 議論トピックの作成・取得 |
| check-in | `check_in` | check-in |

## スキル

| スキル | 説明 |
|--------|------|
| `/man` | 説明します |
| `/ask-compose` | `add_ask`のquestion/contextを構成します |

## 設定
"""


def test_extract_readme_tool_names_reads_only_tool_column():
    names = extract_readme_tool_names(README_TEXT)
    assert names == {"add_topic", "get_topics", "check_in"}


def test_extract_readme_skill_names_ignores_description_backticks():
    # /ask-compose の説明列にある `add_ask` を誤ってスキル名として拾わないこと
    names = extract_readme_skill_names(README_TEXT)
    assert names == {"man", "ask-compose"}


def test_extract_readme_tool_names_returns_none_when_section_missing():
    assert extract_readme_tool_names("# CALM\n\n## スキル\n\n| `/man` | x |\n") is None


def test_extract_skill_dir_names_requires_skill_md():
    paths = [
        "skills/man/SKILL.md",
        "skills/man/references/foo.md",
        "skills/ask-compose/SKILL.md",
        "skills/_shared/helper.py",
    ]
    assert extract_skill_dir_names(paths) == {"man", "ask-compose"}


def test_check_readme_tables_passes_when_sets_match():
    failures, warnings = check_readme_tables(
        README_TEXT,
        tool_names={"add_topic", "get_topics", "check_in"},
        skill_names={"man", "ask-compose"},
    )
    assert failures == []
    assert warnings == []


def test_check_readme_tables_fails_on_missing_tool():
    failures, _ = check_readme_tables(
        README_TEXT,
        tool_names={"add_topic", "get_topics", "check_in", "get_goal"},
        skill_names={"man", "ask-compose"},
    )
    assert len(failures) == 1
    assert "get_goal" in failures[0]


def test_check_readme_tables_fails_on_extra_skill_in_readme():
    failures, _ = check_readme_tables(
        README_TEXT,
        tool_names={"add_topic", "get_topics", "check_in"},
        skill_names={"man"},  # ask-compose はもう存在しない想定
    )
    assert len(failures) == 1
    assert "ask-compose" in failures[0]


def test_check_readme_tables_warns_only_when_readme_missing():
    failures, warnings = check_readme_tables(None, tool_names=set(), skill_names=set())
    assert failures == []
    assert len(warnings) == 1


def test_check_readme_tables_warns_only_when_section_missing():
    text_without_tools_section = "# CALM\n\n## スキル\n\n| `/man` | x |\n"
    failures, warnings = check_readme_tables(
        text_without_tools_section, tool_names={"add_topic"}, skill_names={"man"}
    )
    assert failures == []
    assert len(warnings) == 1


def test_check_readme_tables_reports_both_table_failures_independently():
    failures, _ = check_readme_tables(
        README_TEXT,
        tool_names={"add_topic", "get_topics", "check_in", "get_goal"},  # get_goalがREADMEに無い
        skill_names={"man"},  # ask-composeはもう存在しない想定
    )
    assert len(failures) == 2


# --- main(): head_main_py_for_readme のフォールバック配線 ---


def test_main_fetches_head_main_py_for_readme_when_main_py_not_in_diff(monkeypatch, capsys):
    """src/main.py が diff に含まれない（=1・2のツールIFチェックは走らない）PRでも、
    3のREADME表チェックはheadのsrc/main.pyを別途取得して実行されることを確認する。"""

    def fake_git_show(repo_root, ref, path):
        if path == "src/main.py":
            return HEAD_MAIN_PY_ADDED_TOOL
        if path == lint_doc_cochange.README_PATH:
            return README_TEXT
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(lint_doc_cochange, "git_diff_names", lambda repo_root, base, head: ["README.md"])
    monkeypatch.setattr(lint_doc_cochange, "collect_commit_messages", lambda repo_root, base, head: "")
    monkeypatch.setattr(lint_doc_cochange, "git_show", fake_git_show)
    monkeypatch.setattr(
        lint_doc_cochange,
        "git_ls_tree_paths",
        lambda repo_root, ref, dir_path: ["skills/man/SKILL.md", "skills/ask-compose/SKILL.md"],
    )

    exit_code = lint_doc_cochange.main(["--base", "base-ref", "--head", "head-ref"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "export_material" in err  # HEAD_MAIN_PY_ADDED_TOOLのツールがREADME_TEXTに無い
