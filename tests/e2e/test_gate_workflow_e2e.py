"""`.github/workflows/gate.yml` の "Run gate with base-branch detector" ステップの
実シェルスクリプトを実 git リポジトリに対して実行し、改竄耐性・フォールバック挙動
を検証するE2Eテスト。

ワークフローYAMLに埋め込まれたシェルスクリプトの文字列と env: マッピングを
そのまま抽出して実行する(再実装しない)。これにより、設計とワークフローファイルの
実体との乖離もテストが検出する。

各テストはワークフローが `mktemp -d` で確保する一意な一時ディレクトリに検出器を
取り出すため、固定パスの衝突が無く並列実行しても互いに干渉しない。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_PATH = _PROJECT_ROOT / ".github" / "workflows" / "gate.yml"
_REAL_DETECTOR_PATH = _PROJECT_ROOT / "scripts" / "gate_check.py"


def _extract_gate_step() -> dict:
    workflow = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["gate"]["steps"]
    return next(s for s in steps if s.get("name") == "Run gate with base-branch detector")


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


def _set_remote_tracking_ref(path: Path, branch: str, sha: str) -> None:
    """実際の remote を持たないローカルテストリポジトリに `origin/<branch>` を
    偽装する。ワークフローの `git show origin/<base>:...` が参照解決できるように
    するための最小手段。"""
    subprocess.run(["git", "-C", str(path), "update-ref", f"refs/remotes/origin/{branch}", sha], check=True)


def _run_gate_script(repo: Path, base_ref: str, step_summary: Path) -> subprocess.CompletedProcess:
    step = _extract_gate_step()
    env = dict(os.environ)
    env["GITHUB_STEP_SUMMARY"] = str(step_summary)
    # ステップの env: マッピングをそのまま採用し、GitHub コンテキスト式
    # `${{ github.base_ref }}` だけを実行時の base_ref に解決する。実ランナーが
    # 式を展開して run ブロックへ環境変数を注入するのと同じ経路をなぞる。
    for key, value in (step.get("env") or {}).items():
        env[key] = value.replace("${{ github.base_ref }}", base_ref)
    # GitHub Actions の run ステップは既定で `bash -eo pipefail -c` 相当で実行される
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
    )


def test_gate_script_uses_base_branch_detector_even_if_pr_branch_tampers(tmp_path: Path):
    """PRブランチが scripts/gate_check.py 自体を「常に安全」と嘘をつく検出器に
    書き換えても、CIは origin/main 版(未改竄)の検出器で判定することを確認する。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    real_detector_source = _REAL_DETECTOR_PATH.read_text(encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "gate_check.py").write_text(real_detector_source, encoding="utf-8")
    base_sha = _commit_all(repo, "base: add real detector")
    _set_remote_tracking_ref(repo, "main", base_sha)

    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "pr"], check=True)
    (repo / "scripts" / "gate_check.py").write_text(
        "import json\n"
        "print(json.dumps({'classification': 'post_veto_candidate', 'reason': 'axis_b_met'}))\n",
        encoding="utf-8",
    )
    _commit_all(repo, "pr: tamper detector to always report safe")

    step_summary = tmp_path / "step_summary.md"
    step_summary.write_text("", encoding="utf-8")

    result = _run_gate_script(repo, "main", step_summary)

    assert result.returncode == 0, result.stderr
    verdict = json.loads((repo / "verdict.json").read_text(encoding="utf-8"))
    # 改竄版検出器が自己申告する "post_veto_candidate" ではなく、base 版検出器が
    # scripts/gate_check.py への接触自体を検知して self_protection(pre_go) になる
    assert verdict["classification"] == "pre_go"
    assert verdict["reason"] == "self_protection"
    # job summary にも base 版検出器のレンダリング結果(判定+理由)が書かれる
    summary_text = step_summary.read_text(encoding="utf-8")
    assert "pre_go" in summary_text
    assert "self_protection" in summary_text


def test_gate_script_falls_back_to_pre_go_when_base_lacks_detector(tmp_path: Path):
    """導入初期(検出器がまだ base ブランチにマージされていない)は
    git show が失敗し、pre_go/detector_error のフォールバック verdict で
    ジョブが正常終了(exit 0)することを確認する。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    base_sha = _commit_all(repo, "base: no detector yet")
    _set_remote_tracking_ref(repo, "main", base_sha)

    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "pr"], check=True)
    (repo / "README.md").write_text("hello world\n", encoding="utf-8")
    _commit_all(repo, "pr: unrelated change")

    step_summary = tmp_path / "step_summary.md"
    step_summary.write_text("", encoding="utf-8")

    result = _run_gate_script(repo, "main", step_summary)

    assert result.returncode == 0, result.stderr
    verdict = json.loads((repo / "verdict.json").read_text(encoding="utf-8"))
    assert verdict == {
        "classification": "pre_go",
        "reason": "detector_error",
        "errors": ["detector not on base"],
    }
    # フォールバック経路は --render を実行しないので job summary は空のまま
    assert step_summary.read_text(encoding="utf-8") == ""
