"""scripts/pr_size_check.py の検査ロジックを検証する unit test。

purely-functional な分類ロジック(classify_verdict / _bucket / revert 突き合わせ)は
git を介さない直接呼び出しで検証する。実 diff の計測(build_verdict)は一時 git
リポジトリを使った統合テストとして書く。gh CLI 連携(upsert_pr_comment)は
subprocess.run をモックして検証する。
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

from scripts.pr_size_check import (  # noqa: E402
    LARGE_LINES_MAX,
    PR_SIZE_COMMENT_MARKER,
    build_arg_parser,
    build_verdict,
    check_revert_mismatch,
    classify_verdict,
    main,
    render_comment_body,
    render_text,
    run_ci,
    upsert_pr_comment,
)
from scripts.gate_check import MAX_FILES, MAX_LINES  # noqa: E402


# ---------------------------------------------------------------------------
# ヘルパー(実 git リポジトリ)
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


_REAL_SUBPROCESS_RUN = subprocess.run


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    return repo


def _write(path: Path, rel: str, content: str) -> None:
    target = path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


# ---------------------------------------------------------------------------
# classify_verdict
# ---------------------------------------------------------------------------


def test_classify_verdict_ok_within_both_thresholds():
    assert classify_verdict(MAX_LINES, MAX_FILES) == "ok"


def test_classify_verdict_large_when_lines_exceed_ok_but_within_800():
    assert classify_verdict(MAX_LINES + 1, 1) == "large"
    assert classify_verdict(LARGE_LINES_MAX, 1) == "large"


def test_classify_verdict_large_when_only_files_exceed_ok():
    assert classify_verdict(10, MAX_FILES + 1) == "large"


def test_classify_verdict_oversized_beyond_800_lines():
    assert classify_verdict(LARGE_LINES_MAX + 1, 1) == "oversized"


# ---------------------------------------------------------------------------
# build_verdict(実 git diff)
# ---------------------------------------------------------------------------


def test_build_verdict_classifies_code_test_docs_separately(git_repo: Path):
    _write(git_repo, "src/foo.py", "x = 1\n")
    base_sha = _commit_all(git_repo, "base")

    _write(git_repo, "src/foo.py", "x = 1\ny = 2\n")  # +1 code line
    _write(git_repo, "tests/unit/test_foo.py", "def test_x():\n    assert True\n")  # +2 test lines
    _write(git_repo, "docs/guide.md", "# guide\n")  # +1 doc line
    head_sha = _commit_all(git_repo, "add feature + test + docs")

    verdict = build_verdict(git_repo, base_sha, head_sha)
    assert verdict["lines_code"] == 1
    assert verdict["lines_test"] == 2
    assert verdict["lines_docs"] == 1
    assert verdict["lines_total"] == 4
    assert verdict["files"] == 3
    assert verdict["tests_touched"] is True
    assert verdict["migrations_touched"] is False
    assert verdict["verdict"] == "ok"
    assert verdict["base_ref"] == base_sha
    assert verdict["merge_base"] == base_sha
    assert verdict["head"] == head_sha


def test_build_verdict_excludes_uv_lock_from_lines_and_files(git_repo: Path):
    _write(git_repo, "src/foo.py", "x = 1\n")
    base_sha = _commit_all(git_repo, "base")

    _write(git_repo, "src/foo.py", "x = 2\n")
    _write(git_repo, "uv.lock", "a" * 500 + "\n")
    head_sha = _commit_all(git_repo, "touch code + lockfile")

    verdict = build_verdict(git_repo, base_sha, head_sha)
    assert verdict["files"] == 1
    assert verdict["lines_total"] == 2  # foo.py の1行削除+1行追加のみ


def test_build_verdict_migrations_counted_as_code_and_flagged(git_repo: Path):
    _write(git_repo, "src/foo.py", "x = 1\n")
    base_sha = _commit_all(git_repo, "base")

    _write(git_repo, "migrations/0050_add_x.sql", "ALTER TABLE foo ADD COLUMN x TEXT;\n")
    head_sha = _commit_all(git_repo, "add migration")

    verdict = build_verdict(git_repo, base_sha, head_sha)
    assert verdict["migrations_touched"] is True
    assert verdict["lines_code"] == 1  # migrations/ は tests/docs 対象外なので code に算入
    assert verdict["lines_test"] == 0
    assert verdict["lines_docs"] == 0


def test_build_verdict_oversized_when_code_lines_exceed_800(git_repo: Path):
    _write(git_repo, "src/foo.py", "x = 1\n")
    base_sha = _commit_all(git_repo, "base")

    big_content = "\n".join(f"line_{i} = {i}" for i in range(900)) + "\n"
    _write(git_repo, "src/foo.py", big_content)
    head_sha = _commit_all(git_repo, "large change")

    verdict = build_verdict(git_repo, base_sha, head_sha)
    assert verdict["verdict"] == "oversized"


# ---------------------------------------------------------------------------
# Revert 自己申告の突き合わせ
# ---------------------------------------------------------------------------


def test_check_revert_mismatch_flags_r1_with_migrations_touched():
    body = "## 概要\n\n何か\n\n## Revert\n\nR1\n\n## テスト計画\n\nなし\n"
    note = check_revert_mismatch(body, migrations_touched=True)
    assert note is not None
    assert "R1" in note


def test_check_revert_mismatch_r1_without_migrations_is_fine():
    body = "## Revert\n\nR1\n"
    assert check_revert_mismatch(body, migrations_touched=False) is None


def test_check_revert_mismatch_r2_with_migrations_is_fine():
    body = "## Revert\n\nR2: スナップショット復元で戻す\n"
    assert check_revert_mismatch(body, migrations_touched=True) is None


def test_check_revert_mismatch_untouched_template_guide_is_ambiguous():
    body = (
        "## Revert\n\n<!--\n"
        "R1: migration 非接触。revert commit のみで戻る。\n"
        "R2: migration 接触。戻し方(スナップショット復元 or 逆 migration)を具体的に書く。\n"
        "-->\n"
    )
    assert check_revert_mismatch(body, migrations_touched=True) is None


def test_check_revert_mismatch_no_revert_section_is_none():
    body = "## 概要\n\n何か\n"
    assert check_revert_mismatch(body, migrations_touched=True) is None


def test_check_revert_mismatch_ignores_content_after_next_heading():
    body = "## Revert\n\nR1\n\n## テスト計画\n\nR2 という単語がここにあっても無視される\n"
    note = check_revert_mismatch(body, migrations_touched=True)
    assert note is not None  # R1 のみが Revert セクション内で検出される


# ---------------------------------------------------------------------------
# レンダリング
# ---------------------------------------------------------------------------


def test_render_text_contains_verdict_and_thresholds():
    verdict = {
        "verdict": "ok",
        "lines_code": 10,
        "lines_test": 5,
        "lines_docs": 2,
        "lines_total": 17,
        "files": 3,
        "tests_touched": True,
        "migrations_touched": False,
    }
    text = render_text(verdict)
    assert "verdict: ok" in text
    assert f"ok<={MAX_LINES}" in text
    assert f"ok<={MAX_FILES}" in text


def test_render_comment_body_has_marker_and_valid_json_block():
    verdict = {
        "verdict": "large",
        "lines_code": 500,
        "lines_test": 10,
        "lines_docs": 0,
        "lines_total": 510,
        "files": 4,
        "tests_touched": True,
        "migrations_touched": False,
    }
    body = render_comment_body(verdict, revert_note=None)
    assert body.startswith(PR_SIZE_COMMENT_MARKER)

    fenced = body.split("```json\n", 1)[1].split("\n```", 1)[0]
    parsed = json.loads(fenced)
    assert parsed == verdict


def test_render_comment_body_includes_revert_note_when_present():
    verdict = {
        "verdict": "ok",
        "lines_code": 1,
        "lines_test": 0,
        "lines_docs": 0,
        "lines_total": 1,
        "files": 1,
        "tests_touched": False,
        "migrations_touched": True,
    }
    body = render_comment_body(verdict, revert_note="矛盾があります")
    assert "矛盾があります" in body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_requires_local_or_ci():
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_cli_local_and_ci_are_mutually_exclusive():
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--local", "--ci"])


def test_main_local_json_matches_build_verdict(git_repo: Path, capsys):
    _write(git_repo, "src/foo.py", "x = 1\n")
    base_sha = _commit_all(git_repo, "base")
    _write(git_repo, "src/foo.py", "x = 2\n")
    head_sha = _commit_all(git_repo, "change")

    rc = main(["--local", "--json", "--repo", str(git_repo), "--base", base_sha, "--head", head_sha])
    assert rc == 0
    out = capsys.readouterr().out
    printed = json.loads(out)
    expected = build_verdict(git_repo, base_sha, head_sha)
    assert printed == expected


def test_main_local_never_fails_process_even_when_oversized(git_repo: Path):
    _write(git_repo, "src/foo.py", "x = 1\n")
    base_sha = _commit_all(git_repo, "base")
    big_content = "\n".join(f"line_{i} = {i}" for i in range(900)) + "\n"
    _write(git_repo, "src/foo.py", big_content)
    head_sha = _commit_all(git_repo, "large change")

    rc = main(["--local", "--repo", str(git_repo), "--base", base_sha, "--head", head_sha])
    assert rc == 0


# ---------------------------------------------------------------------------
# --ci モード(gh CLI をモック)
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, stdout: bytes = b""):
        self.stdout = stdout


def test_upsert_pr_comment_creates_new_when_none_exists(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"] and "--paginate" in cmd:
            return _FakeCompletedProcess(stdout=b"[]")
        return _FakeCompletedProcess()

    monkeypatch.setattr("scripts.pr_size_check.subprocess.run", fake_run)
    upsert_pr_comment("owner/repo", 42, "body text")

    post_calls = [c for c in calls if "POST" in c]
    assert len(post_calls) == 1
    assert f"repos/owner/repo/issues/42/comments" in post_calls[0]


def test_upsert_pr_comment_patches_existing(monkeypatch):
    calls: list[list[str]] = []
    existing = [{"id": 999, "body": f"{PR_SIZE_COMMENT_MARKER}\nold"}]

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"] and "--paginate" in cmd:
            return _FakeCompletedProcess(stdout=json.dumps(existing).encode("utf-8"))
        return _FakeCompletedProcess()

    monkeypatch.setattr("scripts.pr_size_check.subprocess.run", fake_run)
    upsert_pr_comment("owner/repo", 42, "new body")

    patch_calls = [c for c in calls if "PATCH" in c]
    assert len(patch_calls) == 1
    assert "repos/owner/repo/issues/comments/999" in patch_calls[0]


def test_run_ci_exit_zero_when_enforce_warn_and_oversized(monkeypatch, git_repo: Path):
    _write(git_repo, "src/foo.py", "x = 1\n")
    base_sha = _commit_all(git_repo, "base")
    big_content = "\n".join(f"line_{i} = {i}" for i in range(900)) + "\n"
    _write(git_repo, "src/foo.py", big_content)
    head_sha = _commit_all(git_repo, "large change")

    event = {"pull_request": {"number": 7, "body": "", "labels": [], "base": {"ref": "main"}}}
    event_path = git_repo / "event.json"
    event_path.write_text(json.dumps(event))

    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PR_SIZE_ENFORCE", "warn")

    def fake_run(cmd, **kwargs):
        if cmd[:1] != ["gh"]:
            return _REAL_SUBPROCESS_RUN(cmd, **kwargs)
        if cmd[:2] == ["gh", "api"] and "--paginate" in cmd:
            return _FakeCompletedProcess(stdout=b"[]")
        return _FakeCompletedProcess()

    monkeypatch.setattr("scripts.pr_size_check.subprocess.run", fake_run)

    parser = build_arg_parser()
    args = parser.parse_args(["--ci", "--repo", str(git_repo), "--base", base_sha, "--head", head_sha])
    rc = run_ci(args)
    assert rc == 0


def test_run_ci_exit_one_when_enforce_fail_and_oversized_without_exempt_label(monkeypatch, git_repo: Path):
    _write(git_repo, "src/foo.py", "x = 1\n")
    base_sha = _commit_all(git_repo, "base")
    big_content = "\n".join(f"line_{i} = {i}" for i in range(900)) + "\n"
    _write(git_repo, "src/foo.py", big_content)
    head_sha = _commit_all(git_repo, "large change")

    event = {"pull_request": {"number": 7, "body": "", "labels": [], "base": {"ref": "main"}}}
    event_path = git_repo / "event.json"
    event_path.write_text(json.dumps(event))

    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PR_SIZE_ENFORCE", "fail")

    def fake_run(cmd, **kwargs):
        if cmd[:1] != ["gh"]:
            return _REAL_SUBPROCESS_RUN(cmd, **kwargs)
        if cmd[:2] == ["gh", "api"] and "--paginate" in cmd:
            return _FakeCompletedProcess(stdout=b"[]")
        return _FakeCompletedProcess()

    monkeypatch.setattr("scripts.pr_size_check.subprocess.run", fake_run)

    parser = build_arg_parser()
    args = parser.parse_args(["--ci", "--repo", str(git_repo), "--base", base_sha, "--head", head_sha])
    rc = run_ci(args)
    assert rc == 1


def test_run_ci_exit_zero_when_enforce_fail_but_size_exempt_label_present(monkeypatch, git_repo: Path):
    _write(git_repo, "src/foo.py", "x = 1\n")
    base_sha = _commit_all(git_repo, "base")
    big_content = "\n".join(f"line_{i} = {i}" for i in range(900)) + "\n"
    _write(git_repo, "src/foo.py", big_content)
    head_sha = _commit_all(git_repo, "large change")

    event = {
        "pull_request": {
            "number": 7,
            "body": "",
            "labels": [{"name": "size-exempt"}],
            "base": {"ref": "main"},
        }
    }
    event_path = git_repo / "event.json"
    event_path.write_text(json.dumps(event))

    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PR_SIZE_ENFORCE", "fail")

    def fake_run(cmd, **kwargs):
        if cmd[:1] != ["gh"]:
            return _REAL_SUBPROCESS_RUN(cmd, **kwargs)
        if cmd[:2] == ["gh", "api"] and "--paginate" in cmd:
            return _FakeCompletedProcess(stdout=b"[]")
        return _FakeCompletedProcess()

    monkeypatch.setattr("scripts.pr_size_check.subprocess.run", fake_run)

    parser = build_arg_parser()
    args = parser.parse_args(["--ci", "--repo", str(git_repo), "--base", base_sha, "--head", head_sha])
    rc = run_ci(args)
    assert rc == 0


def test_run_ci_posts_comment_via_upserted_call(monkeypatch, git_repo: Path):
    _write(git_repo, "src/foo.py", "x = 1\n")
    base_sha = _commit_all(git_repo, "base")
    _write(git_repo, "src/foo.py", "x = 2\n")
    head_sha = _commit_all(git_repo, "small change")

    event = {"pull_request": {"number": 7, "body": "", "labels": [], "base": {"ref": "main"}}}
    event_path = git_repo / "event.json"
    event_path.write_text(json.dumps(event))

    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PR_SIZE_ENFORCE", "warn")

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if cmd[:1] != ["gh"]:
            return _REAL_SUBPROCESS_RUN(cmd, **kwargs)
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"] and "--paginate" in cmd:
            return _FakeCompletedProcess(stdout=b"[]")
        return _FakeCompletedProcess()

    monkeypatch.setattr("scripts.pr_size_check.subprocess.run", fake_run)

    parser = build_arg_parser()
    args = parser.parse_args(["--ci", "--repo", str(git_repo), "--base", base_sha, "--head", head_sha])
    run_ci(args)

    post_calls = [c for c in calls if "POST" in c and "issues/7/comments" in " ".join(c)]
    assert len(post_calls) == 1


def test_run_ci_raises_without_github_event_path(monkeypatch, git_repo: Path):
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    parser = build_arg_parser()
    args = parser.parse_args(["--ci", "--repo", str(git_repo)])
    with pytest.raises(RuntimeError):
        run_ci(args)
