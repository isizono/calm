"""scripts/check_test_removal.py のうち実gitリポジトリを介する
`detect_file_renames` を検証する。

`collect_test_ids` / `main` が行う git worktree + uv sync + pytest --collect-only
の統合的な振る舞いは、依存パッケージのインストールを要し実行コストが高いため
ここでは対象としない。合成リポジトリで高速に検証できる `detect_file_renames`
のみを対象にする。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.check_test_removal import detect_file_renames  # noqa: E402


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
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "test_a.py").write_text(
        "def test_a_one():\n    assert True\n" * 1 + "def test_a_extra():\n    assert True\n" * 20,
        encoding="utf-8",
    )
    (repo / "src.py").write_text("VALUE = 1\n", encoding="utf-8")
    base = _commit_all(repo, "initial")
    return repo


def test_detects_pure_file_rename_under_tests(git_repo: Path):
    base = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    old_path = git_repo / "tests" / "unit" / "test_a.py"
    new_path = git_repo / "tests" / "unit" / "test_a_renamed.py"
    new_path.write_text(old_path.read_text(), encoding="utf-8")
    old_path.unlink()
    head = _commit_all(git_repo, "rename test file")

    renames = detect_file_renames(git_repo, base, head)
    assert renames == {"tests/unit/test_a.py": "tests/unit/test_a_renamed.py"}


def test_in_place_modification_is_not_reported_as_rename(git_repo: Path):
    base = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    path = git_repo / "tests" / "unit" / "test_a.py"
    path.write_text(path.read_text() + "\ndef test_a_two():\n    assert True\n", encoding="utf-8")
    head = _commit_all(git_repo, "add a test")

    assert detect_file_renames(git_repo, base, head) == {}


def test_rename_outside_tests_dir_is_ignored(git_repo: Path):
    base = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    old_path = git_repo / "src.py"
    new_path = git_repo / "src_renamed.py"
    new_path.write_text(old_path.read_text(), encoding="utf-8")
    old_path.unlink()
    head = _commit_all(git_repo, "rename non-test file")

    assert detect_file_renames(git_repo, base, head) == {}


def test_rename_made_independently_on_base_after_head_diverged_is_still_detected(git_repo: Path):
    """headから見て祖先関係にないbase側だけの独立したrenameも検出できることを確認する。

    3ドット(`base...head`)はmerge-base(base, head)からheadへの差分しか見ないため、
    head分岐後にbase側だけで起きたrenameを取りこぼす。2ドット(`base..head`)は
    2つのtree自体を直接比較するため、コミット祖先関係に関係なく検出できる。
    """
    root = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    # head側: test_a.pyには触れず、無関係な変更のみでrootから分岐させる
    (git_repo / "unrelated.txt").write_text("x\n", encoding="utf-8")
    head = _commit_all(git_repo, "unrelated head-side change")

    # base側: rootに戻ってから、head側の履歴には無い独立したrenameを行う
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-q", root], check=True)
    old_path = git_repo / "tests" / "unit" / "test_a.py"
    new_path = git_repo / "tests" / "unit" / "test_a_renamed.py"
    new_path.write_text(old_path.read_text(), encoding="utf-8")
    old_path.unlink()
    base = _commit_all(git_repo, "base-side independent rename")

    renames = detect_file_renames(git_repo, base, head)
    assert renames == {"tests/unit/test_a_renamed.py": "tests/unit/test_a.py"}
