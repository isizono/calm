"""embedding_service._resolve_project_root() のテスト。

A#975 / D#2755 で導入したパス解決ロジック:
  1. env var CALM_PROJECT_ROOT 優先
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
    """CALM_PROJECT_ROOT がセットされていればそれを返す。"""
    monkeypatch.setenv("CALM_PROJECT_ROOT", str(tmp_path))
    result = embedding_service._resolve_project_root()
    assert result == str(tmp_path.resolve())


def test_env_var_resolves_relative(monkeypatch, tmp_path):
    """env var が相対パスでも resolve される。"""
    target = tmp_path / "sub"
    target.mkdir()
    monkeypatch.setenv("CALM_PROJECT_ROOT", str(target))
    result = embedding_service._resolve_project_root()
    assert Path(result) == target.resolve()


def test_git_common_dir_points_to_main_repo_from_worktree(monkeypatch, tmp_path):
    """worktree 配下から呼ばれた `_resolve_project_root()` が main repo を返すことを検証する。

    実装は `cwd=Path(__file__).parent` 固定で git を起動するため、tmp_path 内の擬似 worktree を
    そのまま cwd には切り替えられない。そこで `subprocess.run` をモックして、
    1) `cwd` 引数が `embedding_service.__file__` のあるディレクトリに渡されていること
    2) その git の出力（worktree から見た common-dir）に対して、実装が正しく main repo の
       パスを組み立てて返すこと
    を検証する。
    """
    monkeypatch.delenv("CALM_PROJECT_ROOT", raising=False)

    if not _git_available():
        pytest.skip("git not available")

    # 実 git を使って worktree を作る → そこに対する `git rev-parse --git-common-dir` の
    # 期待出力を取り、モックで実装にそれを返す。
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir()
    _run(["git", "init", "-b", "main"], cwd=main_repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=main_repo)
    _run(["git", "config", "user.name", "test"], cwd=main_repo)
    (main_repo / "README.md").write_text("hello\n")
    _run(["git", "add", "README.md"], cwd=main_repo)
    _run(["git", "commit", "-m", "init"], cwd=main_repo)

    worktree = tmp_path / "worktree"
    _run(
        ["git", "worktree", "add", "-b", "feature/test", str(worktree)],
        cwd=main_repo,
    )

    expected_main_root = main_repo.resolve()

    # 実装が cwd に渡すパス（= embedding_service.py のあるディレクトリ）
    impl_cwd = Path(embedding_service.__file__).parent

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        # worktree 配下から実行したときの git の出力を模す（絶対パス return）
        # 実際の git は `<main_repo>/.git` を絶対パスで返す（worktree からのため）
        completed = subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=str(main_repo.resolve() / ".git") + "\n",
            stderr="",
        )
        return completed

    monkeypatch.setattr(embedding_service.subprocess, "run", fake_run)

    result = embedding_service._resolve_project_root()

    # 1) 正しい cwd で git が呼ばれていること
    assert captured["cmd"] == ["git", "rev-parse", "--git-common-dir"]
    assert Path(captured["cwd"]) == impl_cwd, (
        f"_resolve_project_root は cwd=Path(__file__).parent で git を起動するべき。"
        f" got={captured['cwd']} expected={impl_cwd}"
    )
    # 2) 戻り値が main repo root を指していること（.git の親）
    assert Path(result) == expected_main_root


def test_raises_runtime_error_when_outside_git(monkeypatch, tmp_path):
    """env var なし & git 取得失敗時は RuntimeError を raise する。"""
    monkeypatch.delenv("CALM_PROJECT_ROOT", raising=False)

    # subprocess.run を CalledProcessError を投げるよう差し替える
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(returncode=128, cmd=args[0])

    monkeypatch.setattr(embedding_service.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        embedding_service._resolve_project_root()

    assert "CALM_PROJECT_ROOT" in str(exc_info.value)


def test_raises_runtime_error_when_git_not_found(monkeypatch):
    """git バイナリ自体が無い場合も RuntimeError を raise する。"""
    monkeypatch.delenv("CALM_PROJECT_ROOT", raising=False)

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(embedding_service.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        embedding_service._resolve_project_root()

    assert "git repo" in str(exc_info.value) or "CALM_PROJECT_ROOT" in str(exc_info.value)


def _git_available() -> bool:
    try:
        subprocess.run(
            ["git", "--version"], capture_output=True, check=True
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
