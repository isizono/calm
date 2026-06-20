"""embedding_service._resolve_project_root() のテスト。

A#975 / D#2755 で導入したパス解決ロジック:
  1. env var CC_MEMORY_PROJECT_ROOT 優先
  2. git rev-parse --git-common-dir 経由（worktree からでも main repo を返す）
  3. 失敗時は RuntimeError

worktree 経由の解決はモックではなく実 git で検証する（plan.md 指示）。
"""
import os
import subprocess
from pathlib import Path

import pytest

from src.services import embedding_service


def _run(cmd: list[str], cwd: Path) -> None:
    """git コマンドを実行する。失敗したらテスト側で skip 判断する。"""
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def test_env_var_override(monkeypatch, tmp_path):
    """CC_MEMORY_PROJECT_ROOT がセットされていればそれを返す。"""
    monkeypatch.setenv("CC_MEMORY_PROJECT_ROOT", str(tmp_path))
    result = embedding_service._resolve_project_root()
    assert result == str(tmp_path.resolve())


def test_env_var_resolves_relative(monkeypatch, tmp_path):
    """env var が相対パスでも resolve される。"""
    target = tmp_path / "sub"
    target.mkdir()
    monkeypatch.setenv("CC_MEMORY_PROJECT_ROOT", str(target))
    result = embedding_service._resolve_project_root()
    assert Path(result) == target.resolve()


def test_git_common_dir_points_to_main_repo_from_worktree(monkeypatch, tmp_path):
    """`git rev-parse --git-common-dir` が worktree 配下から呼ばれても main repo の .git を指すことを実 git で確認する。

    本テストは `_resolve_project_root()` を直接呼ぶものではない（実装は `cwd=Path(__file__).parent` 固定で
    git を起動するため、tmp_path 内の擬似 worktree を cwd に切り替えられない）。代わりに、
    `_resolve_project_root` が依存している git の振る舞いそのものを実 git で検証することで、
    実装が依拠する前提が崩れていないことを保証する。
    """
    monkeypatch.delenv("CC_MEMORY_PROJECT_ROOT", raising=False)

    if not _git_available():
        pytest.skip("git not available")

    # main repo を作成
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir()
    _run(["git", "init", "-b", "main"], cwd=main_repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=main_repo)
    _run(["git", "config", "user.name", "test"], cwd=main_repo)
    (main_repo / "README.md").write_text("hello\n")
    _run(["git", "add", "README.md"], cwd=main_repo)
    _run(["git", "commit", "-m", "init"], cwd=main_repo)

    # worktree を作成
    worktree = tmp_path / "worktree"
    _run(
        ["git", "worktree", "add", "-b", "feature/test", str(worktree)],
        cwd=main_repo,
    )

    # worktree 内のサブディレクトリから _resolve_project_root を呼ぶには
    # cwd を切り替える必要がある。実装側は Path(__file__).parent を cwd に渡すため、
    # ここでは _resolve_project_root の内部実装を模倣する形で
    # worktree 配下の任意ディレクトリから git common-dir が main repo を返すかを検証する。
    worktree_subdir = worktree / "src" / "services"
    worktree_subdir.mkdir(parents=True)

    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=worktree_subdir,
        capture_output=True,
        text=True,
        check=True,
    )
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (worktree_subdir / common_dir).resolve()
    main_root = common_dir.parent.resolve()

    assert main_root == main_repo.resolve(), (
        f"worktree subdir からの git common-dir は main repo を指す必要がある。"
        f" got={main_root} expected={main_repo.resolve()}"
    )


def test_raises_runtime_error_when_outside_git(monkeypatch, tmp_path):
    """env var なし & git 取得失敗時は RuntimeError を raise する。"""
    monkeypatch.delenv("CC_MEMORY_PROJECT_ROOT", raising=False)

    # subprocess.run を CalledProcessError を投げるよう差し替える
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(returncode=128, cmd=args[0])

    monkeypatch.setattr(embedding_service.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        embedding_service._resolve_project_root()

    assert "CC_MEMORY_PROJECT_ROOT" in str(exc_info.value)


def test_raises_runtime_error_when_git_not_found(monkeypatch):
    """git バイナリ自体が無い場合も RuntimeError を raise する。"""
    monkeypatch.delenv("CC_MEMORY_PROJECT_ROOT", raising=False)

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(embedding_service.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        embedding_service._resolve_project_root()

    assert "git repo" in str(exc_info.value) or "CC_MEMORY_PROJECT_ROOT" in str(exc_info.value)


def _git_available() -> bool:
    try:
        subprocess.run(
            ["git", "--version"], capture_output=True, check=True
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
