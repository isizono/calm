"""`.github/workflows/gate.yml` の構造を検証するunit test。

YAML構造(トリガー・権限・ステップ構成)を静的に検証する。実際の改竄耐性・
フォールバック挙動は tests/e2e/test_gate_workflow_e2e.py が実 git リポジトリ
を使って検証する。
"""

from __future__ import annotations

from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_PATH = _PROJECT_ROOT / ".github" / "workflows" / "gate.yml"


def _load_workflow() -> dict:
    # YAML の `on:` キーは PyYAML デフォルトでは真偽値 True と解釈される
    # (YAML 1.1 の boolean 短縮形)。既存ワークフローの慣行に合わせて
    # 明示的にラウンドトリップさせず、そのままキーを引く。
    with _WORKFLOW_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_gate_job(workflow: dict) -> dict:
    return workflow["jobs"]["gate"]


def test_workflow_file_exists():
    assert _WORKFLOW_PATH.is_file()


def test_workflow_name_is_gate():
    workflow = _load_workflow()
    assert workflow["name"] == "Gate"


def test_triggers_on_pull_request_open_sync_reopen():
    workflow = _load_workflow()
    # PyYAML は bare `on` を bool True としてパースする
    on_config = workflow.get("on", workflow.get(True))
    assert on_config["pull_request"]["types"] == ["opened", "synchronize", "reopened"]


def test_permissions_are_read_only():
    workflow = _load_workflow()
    assert workflow["permissions"] == {"contents": "read"}


def test_gate_job_runs_on_ubuntu():
    job = _get_gate_job(_load_workflow())
    assert job["runs-on"] == "ubuntu-latest"


def test_checkout_step_has_full_history():
    job = _get_gate_job(_load_workflow())
    checkout_steps = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout")]
    assert len(checkout_steps) == 1
    assert checkout_steps[0]["with"]["fetch-depth"] == 0


def test_gate_step_uses_base_ref_for_detector_extraction():
    job = _get_gate_job(_load_workflow())
    gate_steps = [s for s in job["steps"] if s.get("name") == "Run gate with base-branch detector"]
    assert len(gate_steps) == 1
    run_script = gate_steps[0]["run"]
    # origin/main 版(base_ref)から検出器を取り出す一文が存在する
    assert "git show" in run_script
    assert 'origin/${{ github.base_ref }}:scripts/gate_check.py' in run_script
    # PR head との比較にも base_ref を使う(改竄された worktree 版の base_ref ではなく)
    assert '--base "origin/${{ github.base_ref }}" --head HEAD' in run_script
    # フォールバック時は pre_go/detector_error で正常終了する
    assert '"classification":"pre_go"' in run_script
    assert '"reason":"detector_error"' in run_script
    assert "exit 0" in run_script


def test_upload_artifact_step_publishes_verdict_json():
    job = _get_gate_job(_load_workflow())
    upload_steps = [s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-artifact")]
    assert len(upload_steps) == 1
    assert upload_steps[0]["with"]["name"] == "gate-verdict"
    assert upload_steps[0]["with"]["path"] == "verdict.json"


def test_self_protection_paths_include_gate_workflow():
    """gate.yml 自身への変更が判定を迂回できないことの前提: 検出器の自己保護
    パスリストに `.github/workflows/gate.yml` が含まれていることを確認する。
    """
    import sys

    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
    from scripts.gate_check import DETECTOR_SELF_PATHS

    assert ".github/workflows/gate.yml" in DETECTOR_SELF_PATHS
