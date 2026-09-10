"""src/infra/git_repo.py のユニットテスト

subprocess呼び出し(git rev-parse)を外部境界としてmonkeypatchし、
git-common-dir解決の成否に応じたmain repoルート解決結果を検証する。
"""
import subprocess

from src.infra import git_repo


class TestResolveMainRepoRoot:
    """resolve_main_repo_root(): git-common-dir経由のmain repoルート解決"""

    def test_returns_git_common_dir_parent_when_project_root_is_a_worktree(
        self, monkeypatch, tmp_path,
    ):
        """project_rootがworktreeの場合、git-common-dirから解決したmain repoルートを
        返す(project_root=worktreeルートをそのまま返さない)
        """
        worktree_root = tmp_path / "worktree"
        main_repo_root = tmp_path / "main-repo"
        git_common_dir = main_repo_root / ".git"

        def fake_run(cmd, **kwargs):
            assert cmd == ["git", "rev-parse", "--git-common-dir"]
            assert kwargs["cwd"] == worktree_root
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{git_common_dir}\n", stderr="")

        monkeypatch.setattr(git_repo.subprocess, "run", fake_run)

        result = git_repo.resolve_main_repo_root(worktree_root)

        assert result == main_repo_root.resolve()

    def test_falls_back_to_project_root_when_not_a_git_repository(self, monkeypatch, tmp_path):
        """gitリポジトリでない(プラグインキャッシュ配置)場合はproject_rootをそのまま返す

        このケースこそCALM_PROJECT_ROOT明示設定が元々必要だった配置であり、
        git解決の失敗は「安全にフォールバックすべき」正常系である。
        """
        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(128, cmd, stderr="fatal: not a git repository")

        monkeypatch.setattr(git_repo.subprocess, "run", fake_run)

        assert git_repo.resolve_main_repo_root(tmp_path) == tmp_path

    def test_falls_back_to_project_root_when_git_binary_missing(self, monkeypatch, tmp_path):
        """gitコマンド自体が存在しない環境でもproject_rootをそのまま返す"""
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(git_repo.subprocess, "run", fake_run)

        assert git_repo.resolve_main_repo_root(tmp_path) == tmp_path

    def test_falls_back_to_project_root_on_timeout(self, monkeypatch, tmp_path):
        """git rev-parseがハングした場合も呼び出し元全体をブロックせずproject_rootを返す"""
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(git_repo.subprocess, "run", fake_run)

        assert git_repo.resolve_main_repo_root(tmp_path) == tmp_path
