"""scripts/lint_doc_cochange.py のユニットテスト。

判定ロジック本体（純粋関数）は git subprocess を挟まず直接テストする。
main() の配線テストのみ、git_* 関数を monkeypatch して外部境界（subprocess）を切り離す。
"""
import scripts.lint_doc_cochange as lint_doc_cochange
from scripts.lint_doc_cochange import (
    DB_SCHEMA_DOC,
    check_reference_tables,
    evaluate,
    extract_reference_skill_names,
    extract_reference_tool_names,
    extract_skill_dir_names,
    extract_tool_names,
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

HEAD_MAIN_PY_INVALID_SYNTAX = BASE_MAIN_PY + "\ndef broken(:\n"


# --- extract_tool_names ---


def test_extract_tool_names_finds_only_mcp_tool_functions():
    assert extract_tool_names(BASE_MAIN_PY) == {"add_topic", "get_topics"}


def test_extract_tool_names_returns_none_on_syntax_error():
    assert extract_tool_names(HEAD_MAIN_PY_INVALID_SYNTAX) is None


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
    )
    assert warnings == []
    assert len(failures) == 1
    assert DB_SCHEMA_DOC in failures[0]


def test_evaluate_passes_when_migration_and_schema_doc_both_changed():
    failures, _ = evaluate(
        changed_files=["migrations/0050_add_x.sql", DB_SCHEMA_DOC],
        commit_messages="",
        pr_body="",
    )
    assert failures == []


def test_evaluate_passes_with_exception_marker_in_commit_message():
    failures, _ = evaluate(
        changed_files=["migrations/0050_add_x.sql"],
        commit_messages="chore: index追加\n\n[no-schema-shape-change]",
        pr_body="",
    )
    assert failures == []


def test_evaluate_passes_with_exception_marker_in_pr_body():
    failures, _ = evaluate(
        changed_files=["migrations/0050_add_x.sql"],
        commit_messages="",
        pr_body="## 概要\n...\n[no-schema-shape-change]",
    )
    assert failures == []


def test_evaluate_ignores_non_sql_migrations_dir_changes():
    failures, _ = evaluate(
        changed_files=["migrations/README.md"],
        commit_messages="",
        pr_body="",
    )
    assert failures == []


# --- 参照ドキュメント表パース ---

REFERENCE_TEXT = """# CALM

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


def test_extract_reference_tool_names_reads_only_tool_column():
    names = extract_reference_tool_names(REFERENCE_TEXT)
    assert names == {"add_topic", "get_topics", "check_in"}


def test_extract_reference_skill_names_ignores_description_backticks():
    # /ask-compose の説明列にある `add_ask` を誤ってスキル名として拾わないこと
    names = extract_reference_skill_names(REFERENCE_TEXT)
    assert names == {"man", "ask-compose"}


def test_extract_reference_tool_names_returns_none_when_section_missing():
    assert extract_reference_tool_names("# CALM\n\n## スキル\n\n| `/man` | x |\n") is None


def test_extract_skill_dir_names_requires_skill_md():
    paths = [
        "skills/man/SKILL.md",
        "skills/man/references/foo.md",
        "skills/ask-compose/SKILL.md",
        "skills/_shared/helper.py",
    ]
    assert extract_skill_dir_names(paths) == {"man", "ask-compose"}


def test_check_reference_tables_passes_when_sets_match():
    failures, warnings = check_reference_tables(
        REFERENCE_TEXT,
        tool_names={"add_topic", "get_topics", "check_in"},
        skill_names={"man", "ask-compose"},
    )
    assert failures == []
    assert warnings == []


def test_check_reference_tables_fails_on_missing_tool():
    failures, _ = check_reference_tables(
        REFERENCE_TEXT,
        tool_names={"add_topic", "get_topics", "check_in", "get_goal"},
        skill_names={"man", "ask-compose"},
    )
    assert len(failures) == 1
    assert "get_goal" in failures[0]


def test_check_reference_tables_fails_on_extra_skill_in_reference():
    failures, _ = check_reference_tables(
        REFERENCE_TEXT,
        tool_names={"add_topic", "get_topics", "check_in"},
        skill_names={"man"},  # ask-compose はもう存在しない想定
    )
    assert len(failures) == 1
    assert "ask-compose" in failures[0]


def test_check_reference_tables_warns_only_when_reference_missing():
    failures, warnings = check_reference_tables(None, tool_names=set(), skill_names=set())
    assert failures == []
    assert len(warnings) == 1


def test_check_reference_tables_warns_only_when_section_missing():
    text_without_tools_section = "# CALM\n\n## スキル\n\n| `/man` | x |\n"
    failures, warnings = check_reference_tables(
        text_without_tools_section, tool_names={"add_topic"}, skill_names={"man"}
    )
    assert failures == []
    assert len(warnings) == 1


def test_check_reference_tables_reports_both_table_failures_independently():
    failures, _ = check_reference_tables(
        REFERENCE_TEXT,
        tool_names={"add_topic", "get_topics", "check_in", "get_goal"},  # get_goalがdocs/reference.mdに無い
        skill_names={"man"},  # ask-composeはもう存在しない想定
    )
    assert len(failures) == 2
    assert any("get_goal" in f for f in failures)
    assert any("ask-compose" in f for f in failures)


# --- main(): head_main_py_for_reference のフォールバック配線 ---


def test_main_fetches_head_main_py_for_reference_when_main_py_not_in_diff(monkeypatch, capsys):
    """src/main.py が diff に含まれないPRでも、
    リファレンス表チェックはheadのsrc/main.pyを別途取得して実行されることを確認する。"""

    def fake_git_show(repo_root, ref, path):
        if path == "src/main.py":
            return HEAD_MAIN_PY_ADDED_TOOL
        if path == lint_doc_cochange.REFERENCE_DOC_PATH:
            return REFERENCE_TEXT
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(lint_doc_cochange, "git_diff_names", lambda repo_root, base, head: ["docs/reference.md"])
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
    assert "export_material" in err  # HEAD_MAIN_PY_ADDED_TOOLのツールがREFERENCE_TEXTに無い
