"""scripts/migration_lint.py の破壊的変更 lint を検証する unit test。

大半は git を介さない純粋関数レベルのテスト(合成した migration ファイル文字列を
tmp_path に書いて `lint_file()` に渡す)。`--changed` の git 差分取得のみ、一時 git
リポジトリを使った統合テストとして書く。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.migration_lint import (  # noqa: E402
    GRANDFATHER_MAX_NUMBER,
    Finding,
    LintResult,
    get_changed_migration_files,
    lint_file,
    lint_files,
    lint_ok,
    main,
    split_statements,
)


def _write_migration(tmp_path: Path, name: str, content: str) -> Path:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir(exist_ok=True)
    p = migrations_dir / name
    p.write_text(content, encoding="utf-8")
    return p


def _findings_by_rule(result: LintResult, rule: str) -> list[Finding]:
    return [f for f in result.findings if f.rule == rule]


# ---------------------------------------------------------------------------
# split_statements: コメント除去・BEGIN/CASE...END 深さ管理
# ---------------------------------------------------------------------------


def test_split_statements_splits_on_top_level_semicolon():
    text = "CREATE TABLE foo (id INTEGER);\nCREATE TABLE bar (id INTEGER);\n"
    stmts = split_statements(text)
    assert len(stmts) == 2
    assert "foo" in stmts[0].text
    assert "bar" in stmts[1].text


def test_split_statements_keeps_trigger_body_as_single_statement():
    text = (
        "CREATE TRIGGER trg_x AFTER DELETE ON tags\n"
        "FOR EACH ROW\n"
        "BEGIN\n"
        "    DELETE FROM pins WHERE source_id = OLD.id;\n"
        "    DELETE FROM relations WHERE source_id = OLD.id;\n"
        "END;\n"
    )
    stmts = split_statements(text)
    assert len(stmts) == 1
    assert "DELETE FROM pins" in stmts[0].text
    assert "DELETE FROM relations" in stmts[0].text


def test_split_statements_handles_case_when_end_without_breaking_split():
    text = "UPDATE tags SET namespace = CASE WHEN namespace = 'mode' THEN 'intent' ELSE namespace END WHERE 1=1;\nCREATE TABLE bar (id INTEGER);\n"
    stmts = split_statements(text)
    assert len(stmts) == 2
    assert "CASE WHEN" in stmts[0].text
    assert "bar" in stmts[1].text


def test_split_statements_ignores_semicolon_inside_string_literal():
    text = "UPDATE t SET name = 'a;b' WHERE id = 1;\n"
    stmts = split_statements(text)
    assert len(stmts) == 1
    assert stmts[0].text.strip().startswith("UPDATE t")


def test_split_statements_strips_comments_from_stripped_text():
    text = "-- depends: 0001_x\n-- DROP TABLE 的な話をコメントで書いてみる\nCREATE TABLE foo (id INTEGER);\n"
    stmts = split_statements(text)
    assert len(stmts) == 1
    assert stmts[0].start_line == 1
    assert "DROP TABLE" not in stmts[0].stripped_text
    assert "CREATE TABLE foo" in stmts[0].stripped_text


# ---------------------------------------------------------------------------
# 破壊的ルール検出(新規ファイル・宣言なし → error)
# ---------------------------------------------------------------------------


def _new_migration_header(number: int = GRANDFATHER_MAX_NUMBER + 1) -> str:
    return f"-- depends: {GRANDFATHER_MAX_NUMBER:04d}_prev\n"


def test_drop_table_without_declaration_fails(tmp_path):
    content = _new_migration_header() + "DROP TABLE foo;\n"
    p = _write_migration(tmp_path, "0049_drop_foo.sql", content)
    result = lint_file(p)
    assert not lint_ok(result)
    findings = _findings_by_rule(result, "drop-table")
    assert len(findings) == 1
    assert findings[0].severity == "error"


def test_drop_table_with_declaration_downgrades_to_info(tmp_path):
    content = "-- destructive: 不要になった foo テーブルを削除\n" + _new_migration_header() + "DROP TABLE foo;\n"
    p = _write_migration(tmp_path, "0049_drop_foo.sql", content)
    result = lint_file(p)
    assert lint_ok(result)
    assert result.destructive_declared is True
    assert result.is_destructive is True
    findings = _findings_by_rule(result, "drop-table")
    assert len(findings) == 1
    assert findings[0].severity == "info"


def test_drop_table_downgrades_to_table_rebuild_when_rebuild_pattern_present(tmp_path):
    content = (
        _new_migration_header()
        + "CREATE TABLE foo_new (id INTEGER PRIMARY KEY, name TEXT);\n"
        + "INSERT INTO foo_new (id, name) SELECT id, name FROM foo;\n"
        + "DROP TABLE foo;\n"
        + "ALTER TABLE foo_new RENAME TO foo;\n"
    )
    p = _write_migration(tmp_path, "0049_rebuild_foo.sql", content)
    result = lint_file(p)
    assert lint_ok(result)  # table-rebuild は warn 級のため宣言不要
    assert _findings_by_rule(result, "drop-table") == []
    rebuild_findings = _findings_by_rule(result, "table-rebuild")
    assert len(rebuild_findings) == 1
    assert rebuild_findings[0].severity == "warn"


def test_drop_column_without_declaration_fails(tmp_path):
    content = _new_migration_header() + "ALTER TABLE foo DROP COLUMN bar;\n"
    p = _write_migration(tmp_path, "0049_drop_col.sql", content)
    result = lint_file(p)
    assert not lint_ok(result)
    assert len(_findings_by_rule(result, "drop-column")) == 1


def test_delete_from_without_declaration_fails(tmp_path):
    content = _new_migration_header() + "DELETE FROM foo WHERE id = 1;\n"
    p = _write_migration(tmp_path, "0049_delete_foo.sql", content)
    result = lint_file(p)
    assert not lint_ok(result)
    assert len(_findings_by_rule(result, "delete-from")) == 1


def test_delete_from_sqlite_sequence_is_excluded(tmp_path):
    content = _new_migration_header() + "DELETE FROM sqlite_sequence WHERE name = 'foo';\n"
    p = _write_migration(tmp_path, "0049_reset_seq.sql", content)
    result = lint_file(p)
    assert lint_ok(result)
    assert _findings_by_rule(result, "delete-from") == []


def test_update_without_where_fails(tmp_path):
    content = _new_migration_header() + "UPDATE foo SET name = 'x';\n"
    p = _write_migration(tmp_path, "0049_update_all.sql", content)
    result = lint_file(p)
    assert not lint_ok(result)
    assert len(_findings_by_rule(result, "update-without-where")) == 1


def test_update_with_where_passes(tmp_path):
    content = _new_migration_header() + "UPDATE foo SET name = 'x' WHERE id = 1;\n"
    p = _write_migration(tmp_path, "0049_update_one.sql", content)
    result = lint_file(p)
    assert lint_ok(result)
    assert _findings_by_rule(result, "update-without-where") == []


def test_delete_and_update_inside_trigger_body_are_not_flagged(tmp_path):
    content = (
        _new_migration_header()
        + "CREATE TRIGGER trg_cascade AFTER DELETE ON foo\n"
        + "FOR EACH ROW\n"
        + "BEGIN\n"
        + "    DELETE FROM bar WHERE foo_id = OLD.id;\n"
        + "    UPDATE baz SET foo_id = NULL WHERE foo_id = OLD.id;\n"
        + "END;\n"
    )
    p = _write_migration(tmp_path, "0049_trigger.sql", content)
    result = lint_file(p)
    assert lint_ok(result)
    assert result.is_destructive is False
    assert result.findings == []


# ---------------------------------------------------------------------------
# lint-ok による個別ルール抑制
# ---------------------------------------------------------------------------


def test_lint_ok_suppresses_only_the_named_rule(tmp_path):
    content = (
        _new_migration_header()
        + "-- lint-ok: delete-from バックフィル用の一時データ削除で意図的\n"
        + "DELETE FROM foo WHERE id = 1;\n"
        + "ALTER TABLE foo DROP COLUMN bar;\n"
    )
    p = _write_migration(tmp_path, "0049_mixed.sql", content)
    result = lint_file(p)
    assert not lint_ok(result)  # drop-column はまだ未宣言で error
    delete_findings = _findings_by_rule(result, "delete-from")
    assert delete_findings[0].severity == "info"
    drop_col_findings = _findings_by_rule(result, "drop-column")
    assert drop_col_findings[0].severity == "error"


# ---------------------------------------------------------------------------
# missing-depends: 宣言では免除されない
# ---------------------------------------------------------------------------


def test_missing_depends_header_fails_even_with_destructive_declaration(tmp_path):
    content = "-- destructive: 意図的な削除\nDROP TABLE foo;\n"
    p = _write_migration(tmp_path, "0049_no_depends.sql", content)
    result = lint_file(p)
    assert not lint_ok(result)
    findings = _findings_by_rule(result, "missing-depends")
    assert len(findings) == 1
    assert findings[0].severity == "error"


def test_depends_header_with_empty_value_is_accepted(tmp_path):
    # 0001 相当(ルートmigrationはdepends値が空でも良い)
    content = "-- depends:\nCREATE TABLE foo (id INTEGER);\n"
    p = _write_migration(tmp_path, "0049_root_like.sql", content)
    result = lint_file(p)
    assert _findings_by_rule(result, "missing-depends") == []


# ---------------------------------------------------------------------------
# duplicate-number
# ---------------------------------------------------------------------------


def test_duplicate_number_between_two_new_files_is_warn_and_non_blocking(tmp_path):
    header = _new_migration_header()
    p1 = _write_migration(tmp_path, "0049_a.sql", header + "CREATE TABLE a (id INTEGER);\n")
    p2 = _write_migration(tmp_path, "0049_b.sql", header + "CREATE TABLE b (id INTEGER);\n")
    r1 = lint_file(p1)
    r2 = lint_file(p2)
    assert lint_ok(r1) and lint_ok(r2)
    assert len(_findings_by_rule(r1, "duplicate-number")) == 1
    assert _findings_by_rule(r1, "duplicate-number")[0].severity == "warn"
    assert len(_findings_by_rule(r2, "duplicate-number")) == 1


def test_duplicate_number_not_flagged_for_grandfathered_files(tmp_path):
    p1 = _write_migration(tmp_path, "0005_a.sql", "-- depends:\nCREATE TABLE a (id INTEGER);\n")
    p2 = _write_migration(tmp_path, "0005_b.sql", "-- depends:\nCREATE TABLE b (id INTEGER);\n")
    r1 = lint_file(p1)
    r2 = lint_file(p2)
    assert _findings_by_rule(r1, "duplicate-number") == []
    assert _findings_by_rule(r2, "duplicate-number") == []


def test_duplicate_number_not_flagged_when_no_collision(tmp_path):
    header = _new_migration_header()
    p = _write_migration(tmp_path, "0049_solo.sql", header + "CREATE TABLE solo (id INTEGER);\n")
    result = lint_file(p)
    assert _findings_by_rule(result, "duplicate-number") == []


# ---------------------------------------------------------------------------
# grandfather: 既存 52 ファイル全てが lint --all を通る
# ---------------------------------------------------------------------------


def test_all_existing_migrations_pass_lint():
    migrations_dir = _PROJECT_ROOT / "migrations"
    paths = sorted(migrations_dir.glob("*.sql"))
    assert len(paths) >= 52  # 本 PR 時点の既存ファイル数を下回っていないことの確認
    results = lint_files(paths)
    failing = [(r.path, [f for f in r.findings if f.severity == "error"]) for r in results if not lint_ok(r)]
    assert failing == [], f"grandfather対象のはずの既存migrationがlintで失敗: {failing}"


def test_missing_depends_never_hits_existing_migrations():
    migrations_dir = _PROJECT_ROOT / "migrations"
    paths = sorted(migrations_dir.glob("*.sql"))
    results = lint_files(paths)
    for r in results:
        assert _findings_by_rule(r, "missing-depends") == [], r.path


def test_known_duplicate_number_groups_are_grandfathered_and_silent():
    migrations_dir = _PROJECT_ROOT / "migrations"
    for number in ("0005", "0015", "0039", "0046"):
        matches = sorted(migrations_dir.glob(f"{number}_*.sql"))
        assert len(matches) == 2, f"{number} の重複ペアが想定と異なる: {matches}"
        for p in matches:
            result = lint_file(p)
            assert _findings_by_rule(result, "duplicate-number") == []


def test_table_rebuild_pattern_detected_on_real_0039_file():
    p = _PROJECT_ROOT / "migrations" / "0039_extend_tag_namespace.sql"
    result = lint_file(p)
    assert _findings_by_rule(result, "drop-table") == []
    assert len(_findings_by_rule(result, "table-rebuild")) == 1


def test_real_drop_column_migration_is_grandfathered():
    p = _PROJECT_ROOT / "migrations" / "0047_drop_decisions_logs_topic_id.sql"
    result = lint_file(p)
    assert lint_ok(result)
    findings = _findings_by_rule(result, "drop-column")
    assert len(findings) == 2  # decisions.topic_id / discussion_logs.topic_id
    assert all(f.severity == "info" for f in findings)
    assert result.is_destructive is True
    assert result.destructive_declared is False  # 宣言ヘッダは無い。grandfatherのみで通る


# ---------------------------------------------------------------------------
# --changed: git 差分取得(統合テスト)
# ---------------------------------------------------------------------------


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


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "migrations").mkdir()
    (repo / "migrations" / "0048_existing.sql").write_text("-- depends: 0047_prev\nCREATE TABLE existing (id INTEGER);\n", encoding="utf-8")
    _commit_all(repo, "initial")
    subprocess.run(["git", "-C", str(repo), "branch", "-f", "origin/main", "HEAD"], check=True)
    return repo


def test_get_changed_migration_files_detects_new_file(git_repo: Path):
    new_file = git_repo / "migrations" / "0049_new.sql"
    new_file.write_text("-- depends: 0048_existing\nCREATE TABLE new_table (id INTEGER);\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature"], check=True)
    _commit_all(git_repo, "add new migration")

    changed = get_changed_migration_files(git_repo, base_ref="origin/main", head_ref="HEAD")
    assert [p.name for p in changed] == ["0049_new.sql"]


def test_get_changed_migration_files_empty_when_no_migration_diff(git_repo: Path):
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature"], check=True)
    (git_repo / "README.md").write_text("hello\n", encoding="utf-8")
    _commit_all(git_repo, "unrelated change")

    changed = get_changed_migration_files(git_repo, base_ref="origin/main", head_ref="HEAD")
    assert changed == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_all_exits_zero_on_real_migrations_dir():
    exit_code = main(["--all", "--repo", str(_PROJECT_ROOT)])
    assert exit_code == 0


def test_main_changed_exits_nonzero_when_new_undeclared_destructive_migration(git_repo: Path, capsys):
    new_file = git_repo / "migrations" / "0049_bad.sql"
    new_file.write_text("-- depends: 0048_existing\nDROP TABLE existing;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature"], check=True)
    _commit_all(git_repo, "add destructive migration")

    exit_code = main(["--changed", "--repo", str(git_repo), "--base", "origin/main"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "drop-table" in captured.out


def test_main_changed_exits_zero_when_new_migration_is_declared(git_repo: Path):
    new_file = git_repo / "migrations" / "0049_ok.sql"
    new_file.write_text(
        "-- destructive: 意図的なテーブル削除\n-- depends: 0048_existing\nDROP TABLE existing;\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature"], check=True)
    _commit_all(git_repo, "add declared destructive migration")

    exit_code = main(["--changed", "--repo", str(git_repo), "--base", "origin/main"])
    assert exit_code == 0
