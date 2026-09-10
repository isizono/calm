"""git worktree からmain repoルートを解決する共有ユーティリティ。

`launcher.py`・`restart_service.py` の双方が、実行時cwdやproject_rootが
worktree配下を指しうる状況で、正規化されたmain repoルートを必要とする。
両者が独立した実装を持つと解決ロジックが乖離するため、ここに集約する。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

SUBPROCESS_TIMEOUT_SEC = 5.0


def resolve_main_repo_root(
    project_root: Path, *, timeout_sec: float = SUBPROCESS_TIMEOUT_SEC
) -> Path:
    """project_rootをgit-common-dir経由で検証し、main repoルートを返す。

    project_rootがworktree配下を指す場合、`git rev-parse --git-common-dir`は
    main repoの`.git`ディレクトリを返すため、その親をmain repoルートとして返す
    (worktreeルートをそのまま返さない)。

    gitリポジトリでない場合(例: プラグインキャッシュ配置)はgit解決自体が失敗する
    ため、project_rootをそのまま返す。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True,
            cwd=project_root, timeout=timeout_sec,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return project_root
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (project_root / common_dir).resolve()
    return common_dir.parent.resolve()
