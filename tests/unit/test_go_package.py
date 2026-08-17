"""scripts/go_package.py の単体テスト。

テンプレート整合・lintの各規則(L1-L8)・機械可読ブロックの抽出・shadow集計の
純粋関数を中心に検証する。`new` サブコマンドの gate_check.sh 連携は実 git
リポジトリを使った統合テストとして書く(subprocess呼び出しの実挙動を担保する)。
shadow-report の DB 連携は tests/conftest.py の `temp_db` フィクスチャ +
実サービス層(add_material)で作った material を対象に確認する。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.go_package import (  # noqa: E402
    REQUIRED_SECTIONS,
    SECTION_BEHAVIOR,
    SECTION_BLAST,
    SECTION_DEPENDENCY,
    SECTION_FAILURE_MODE,
    SECTION_INVARIANT,
    SECTION_MAP_UPDATE,
    SECTION_NOVEL,
    SECTION_PRECEDENTS,
    SECTION_REVERT,
    SECTION_TEST_GUARANTEE,
    aggregate_shadow_report,
    build_machine_block,
    compute_missing_packages_for_prs,
    expected_divergence,
    extract_machine_block,
    find_machine_block_yaml,
    lint_document,
    load_pull_json,
    main,
    parse_sections,
    render_document,
    render_yaml_block,
    run_gate_check,
)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _base_block(**overrides) -> dict:
    block = build_machine_block(
        activity=1,
        machine_classification="post_veto_candidate",
        detector_sha256="deadbeef",
        verdict_sha256="cafef00d",
        predicted=None,
        presented="unavailable",
        guarantee=None,
    )
    for path, value in overrides.items():
        keys = path.split(".")
        target = block
        for k in keys[:-1]:
            target = target[k]
        target[keys[-1]] = value
    return block


def _filled_doc(machine_block: dict, section_overrides: dict[str, str] | None = None, include_shadow_defaults: bool = True) -> str:
    """strict lint(--mode shadowかつプレースホルダ不許容)を通る完全なドキュメントを組み立てる。"""
    gate_render = f"### {SECTION_BLAST}\n- 判定: dummy\n\n### {SECTION_REVERT}\n- 変更規模: dummy\n"

    precedents = machine_block.get("precedents") or []
    rows = "\n".join(f"| {p['ref']} | 要約 | {p['stance']} | 根拠 |" for p in precedents)
    table = "| 判例 | 内容要約 | 適用判断 | 根拠 |\n|---|---|---|---|"
    if rows:
        table += "\n" + rows

    sections = {
        SECTION_NOVEL: "なし",
        SECTION_INVARIANT: "invariant本文",
        SECTION_BEHAVIOR: "挙動差本文",
        SECTION_DEPENDENCY: "なし",
        SECTION_MAP_UPDATE: "地図更新本文",
        SECTION_TEST_GUARANTEE: "テスト本文",
        SECTION_FAILURE_MODE: "故障モード本文",
    }
    if section_overrides:
        sections.update(section_overrides)

    yaml_text = render_yaml_block(machine_block)
    body = (
        "## 1-a 分類判定材料\n\n"
        f"{gate_render}\n"
        f"### {SECTION_PRECEDENTS}\n{table}\n\n"
        f"### {SECTION_NOVEL}\n{sections[SECTION_NOVEL]}\n\n"
        "## 1-b 地図メンテ材料\n\n"
        f"### {SECTION_INVARIANT}\n{sections[SECTION_INVARIANT]}\n\n"
        f"### {SECTION_BEHAVIOR}\n{sections[SECTION_BEHAVIOR]}\n\n"
        f"### {SECTION_DEPENDENCY}\n{sections[SECTION_DEPENDENCY]}\n\n"
        f"### {SECTION_MAP_UPDATE}\n{sections[SECTION_MAP_UPDATE]}\n\n"
        "## 1-c 品質証跡\n\n"
        f"### {SECTION_TEST_GUARANTEE}\n{sections[SECTION_TEST_GUARANTEE]}\n\n"
        f"### {SECTION_FAILURE_MODE}\n{sections[SECTION_FAILURE_MODE]}\n"
    )
    return f"# GO判定: test\n\n```go-package\n{yaml_text}\n```\n\n{body}"


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
def gate_repos(tmp_path: Path) -> Path:
    """origin/main に本物の gate_check.py を持つ、独立した git リポジトリ2つ
    (origin/work)を用意する。work は origin を remote に持つ。"""
    origin = tmp_path / "origin"
    origin.mkdir()
    _init_repo(origin)
    (origin / "scripts").mkdir()
    real_gate_check = (_PROJECT_ROOT / "scripts" / "gate_check.py").read_text(encoding="utf-8")
    (origin / "scripts" / "gate_check.py").write_text(real_gate_check, encoding="utf-8")
    _commit_all(origin, "base with detector")

    work = tmp_path / "work"
    work.mkdir()
    _init_repo(work)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(["git", "-C", str(work), "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", str(work), "merge", "-q", "origin/main"], check=True)
    return work


# ---------------------------------------------------------------------------
# template / render_document の整合(テストは lint --allow-placeholder 経由)
# ---------------------------------------------------------------------------


def test_template_command_output_has_go_package_fence(capsys):
    rc = main(["template"])
    assert rc == 0
    out = capsys.readouterr().out
    assert find_machine_block_yaml(out) is not None


def test_template_output_passes_lint_with_allow_placeholder(capsys):
    main(["template"])
    text = capsys.readouterr().out
    result = lint_document(text, mode="shadow", allow_placeholder=True)
    assert result.errors == []
    assert result.exit_code == 0


def test_template_output_fails_lint_without_allow_placeholder(capsys):
    main(["template"])
    text = capsys.readouterr().out
    result = lint_document(text, mode="shadow", allow_placeholder=False)
    assert result.exit_code == 1
    assert any("L2" in e for e in result.errors)
    assert any("L6" in e for e in result.errors)


def test_template_output_all_required_sections_present(capsys):
    main(["template"])
    text = capsys.readouterr().out
    from scripts.go_package import _body_after_fence

    sections = parse_sections(_body_after_fence(text))
    for canonical in REQUIRED_SECTIONS:
        assert canonical in sections


# ---------------------------------------------------------------------------
# extract / round-trip
# ---------------------------------------------------------------------------


def test_render_document_round_trips_through_extract():
    block = _base_block(precedents=[{"ref": "decision 5", "stance": "informational", "note": "参考"}])
    gate_render_md = f"### {SECTION_BLAST}\n- 判定: pre_go(axis_a_hit)\n\n### {SECTION_REVERT}\n- 変更規模: 1行\n"
    doc = render_document(block, gate_render_md, title="テスト変更")
    assert "# GO判定: テスト変更" in doc
    data, err = extract_machine_block(doc)
    assert err is None
    assert data == block
    assert "- 判定: pre_go(axis_a_hit)" in doc


def test_extract_round_trips_yaml_to_json(capsys, tmp_path: Path):
    block = _base_block()
    doc = _filled_doc(block)
    f = tmp_path / "pkg.md"
    f.write_text(doc, encoding="utf-8")

    rc = main(["extract", str(f)])
    assert rc == 0
    out = capsys.readouterr().out
    extracted = json.loads(out)
    assert extracted == block


def test_extract_missing_fence_returns_error():
    data, err = extract_machine_block("# no fence here\n")
    assert data is None
    assert err is not None and "L1" in err


def test_extract_invalid_yaml_returns_error():
    text = "```go-package\nkey: [unclosed\n```\n"
    data, err = extract_machine_block(text)
    assert data is None
    assert err is not None and "L1" in err


# ---------------------------------------------------------------------------
# lint: L1
# ---------------------------------------------------------------------------


def test_lint_l1_missing_fence_is_error():
    result = lint_document("# no fence\n", mode="shadow", allow_placeholder=True)
    assert any("L1" in e for e in result.errors)


def test_lint_l1_unknown_schema_version_is_error():
    block = _base_block(**{"schema_version": 999})
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert any("L1" in e and "schema_version" in e for e in result.errors)


# ---------------------------------------------------------------------------
# lint: L2
# ---------------------------------------------------------------------------


def test_lint_l2_missing_section_is_error():
    block = _base_block()
    doc = _filled_doc(block)
    # invariant セクション見出しごと削除する
    doc = doc.replace(f"### {SECTION_INVARIANT}\ninvariant本文\n\n", "")
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert any("L2" in e and SECTION_INVARIANT in e for e in result.errors)


def test_lint_l2_empty_section_is_error_without_placeholder():
    block = _base_block()
    doc = _filled_doc(block, section_overrides={SECTION_MAP_UPDATE: "<!-- 未記入 -->"})
    result = lint_document(doc, mode="shadow", allow_placeholder=False)
    assert any("L2" in e and SECTION_MAP_UPDATE in e for e in result.errors)


def test_lint_l2_empty_section_allowed_with_placeholder():
    block = _base_block()
    doc = _filled_doc(block, section_overrides={SECTION_MAP_UPDATE: "<!-- 未記入 -->"})
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert not any("L2" in e and SECTION_MAP_UPDATE in e for e in result.errors)


# ---------------------------------------------------------------------------
# lint: L3
# ---------------------------------------------------------------------------


def test_lint_l3_invalid_stance_is_error():
    block = _base_block(precedents=[{"ref": "decision 1", "stance": "not_a_real_stance", "note": "x"}])
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert any("L3" in e and "stance" in e for e in result.errors)


def test_lint_l3_row_count_matches_is_ok():
    block = _base_block(precedents=[{"ref": "decision 1", "stance": "applied", "note": "x"}])
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert not any("L3" in e for e in result.errors)


def test_lint_l3_row_count_mismatch_is_error():
    block = _base_block(precedents=[{"ref": "decision 1", "stance": "applied", "note": "x"}])
    doc = _filled_doc(block)
    # 判例引用テーブルに手動で1行追加し、YAML側(1件)とズレさせる
    doc = doc.replace(
        "|---|---|---|---|\n| decision 1 | 要約 | applied | 根拠 |",
        "|---|---|---|---|\n| decision 1 | 要約 | applied | 根拠 |\n| decision 2 | 要約 | applied | 根拠 |",
    )
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert any("L3" in e and "不一致" in e for e in result.errors)


# ---------------------------------------------------------------------------
# lint: L4
# ---------------------------------------------------------------------------


def test_lint_l4_missing_novel_points_key_is_error():
    block = _base_block()
    doc = _filled_doc(block)
    yaml_text = render_yaml_block(block)
    without_novel = "\n".join(line for line in yaml_text.split("\n") if not line.startswith("novel_points"))
    doc = doc.replace(yaml_text, without_novel)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert any("L4" in e for e in result.errors)


def test_lint_l4_novel_points_present_empty_list_is_ok():
    block = _base_block()  # novel_points はデフォルトで [] (キーは存在する)
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert not any("L4" in e for e in result.errors)


# ---------------------------------------------------------------------------
# lint: L5(一方向性)
# ---------------------------------------------------------------------------


def test_lint_l5_pre_go_machine_to_post_veto_effective_is_always_error():
    block = _base_block(**{"gate.machine": "pre_go", "gate.effective": "post_veto_candidate"})
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert any("L5" in e for e in result.errors)


def test_lint_l5_post_veto_machine_to_pre_go_effective_requires_escalated_by():
    block = _base_block(**{"gate.machine": "post_veto_candidate", "gate.effective": "pre_go", "gate.escalated_by": None})
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert any("L5" in e and "escalated_by" in e for e in result.errors)


def test_lint_l5_post_veto_machine_to_pre_go_effective_passes_with_escalated_by():
    block = _base_block(
        **{
            "gate.machine": "post_veto_candidate",
            "gate.effective": "pre_go",
            "gate.escalated_by": "self-report: 疑わしい",
        }
    )
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert not any("L5" in e for e in result.errors)


def test_lint_l5_gray_resolution_exception_allows_post_veto():
    block = _base_block(
        precedents=[{"ref": "decision 42", "stance": "applied", "note": "根拠"}],
        **{
            "gate.machine": "gray",
            "gate.effective": "post_veto_candidate",
            "gray_resolution": {"resolved_to": "post_veto_candidate", "basis": [{"type": "decision", "id": 42}]},
        },
    )
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert not any("L5" in e for e in result.errors)


def test_lint_l5_gray_resolution_exception_allows_ref_format_variations():
    """ref の表記ゆれ(大文字・ハイフン区切り)があっても basis との一致判定は通る"""
    block = _base_block(
        precedents=[{"ref": "Decision-42", "stance": "applied", "note": "根拠"}],
        **{
            "gate.machine": "gray",
            "gate.effective": "post_veto_candidate",
            "gray_resolution": {"resolved_to": "post_veto_candidate", "basis": [{"type": "decision", "id": 42}]},
        },
    )
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert not any("L5" in e for e in result.errors)


def test_lint_l5_gray_resolution_without_basis_citation_is_error():
    block = _base_block(
        precedents=[],
        **{
            "gate.machine": "gray",
            "gate.effective": "post_veto_candidate",
            "gray_resolution": {"resolved_to": "post_veto_candidate", "basis": [{"type": "decision", "id": 42}]},
        },
    )
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert any("L5" in e for e in result.errors)


def test_lint_l5_gray_machine_to_gray_effective_needs_no_exception():
    block = _base_block(**{"gate.machine": "gray", "gate.effective": "gray"})
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert not any("L5" in e for e in result.errors)


# ---------------------------------------------------------------------------
# lint: L6(shadow整合)
# ---------------------------------------------------------------------------


def test_lint_l6_shadow_block_required_in_shadow_mode_strict():
    block = _base_block()
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=False)
    assert any("L6" in e and "必須" in e for e in result.errors)


def test_lint_l6_shadow_block_not_required_when_placeholder_allowed():
    block = _base_block()
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert not any("L6" in e and "必須" in e for e in result.errors)


@pytest.mark.parametrize(
    "machine,human,expected",
    [
        ("post_veto_candidate", "post_veto_candidate", "none"),
        ("pre_go", "pre_go", "none"),
        ("post_veto_candidate", "pre_go", "false_negative"),
        ("pre_go", "post_veto_candidate", "false_positive"),
        ("gray", "pre_go", "gray_case"),
        ("gray", "post_veto_candidate", "gray_case"),
    ],
)
def test_expected_divergence_matches_3_7_table(machine, human, expected):
    assert expected_divergence(machine, human) == expected


def test_lint_l6_correct_divergence_passes():
    block = _base_block(
        **{"gate.machine": "post_veto_candidate"},
        shadow={"human": "post_veto_candidate", "divergence": "none", "divergence_reason": None},
    )
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert not any("L6" in e for e in result.errors)


def test_lint_l6_wrong_divergence_is_error():
    block = _base_block(
        **{"gate.machine": "post_veto_candidate"},
        shadow={"human": "pre_go", "divergence": "none", "divergence_reason": None},  # 本来は false_negative
    )
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert any("L6" in e and "導出" in e for e in result.errors)


def test_lint_l6_invalid_human_value_is_error():
    block = _base_block(shadow={"human": "gray", "divergence": "gray_case", "divergence_reason": None})
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert any("L6" in e and "human" in e for e in result.errors)


# ---------------------------------------------------------------------------
# lint: L7(警告)
# ---------------------------------------------------------------------------


def test_lint_l7_predicted_machine_drift_is_warning_not_error():
    block = _base_block(**{"gate.predicted": "pre_go", "gate.machine": "post_veto_candidate"})
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert any("L7" in w for w in result.warnings)
    assert not any("L7" in e for e in result.errors)


def test_lint_l7_no_warning_when_predicted_matches_machine():
    block = _base_block(**{"gate.predicted": "post_veto_candidate", "gate.machine": "post_veto_candidate"})
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert not any("L7" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# lint: L8
# ---------------------------------------------------------------------------


def test_lint_l8_live_mode_requires_pull_presented():
    block = _base_block()  # presented は "unavailable" のまま
    doc = _filled_doc(block)
    result = lint_document(doc, mode="live", allow_placeholder=True)
    assert any("L8" in e for e in result.errors)


def test_lint_l8_live_mode_passes_when_presented_is_list():
    block = _base_block(**{"pull.presented": [{"type": "decision", "id": 1}]})
    doc = _filled_doc(block)
    result = lint_document(doc, mode="live", allow_placeholder=True)
    assert not any("L8" in e for e in result.errors)


def test_lint_l8_shadow_mode_ignores_pull_presented():
    block = _base_block()  # presented は "unavailable"
    doc = _filled_doc(block)
    result = lint_document(doc, mode="shadow", allow_placeholder=True)
    assert not any("L8" in e for e in result.errors)


# ---------------------------------------------------------------------------
# load_pull_json
# ---------------------------------------------------------------------------


def test_load_pull_json_none_path_returns_unavailable():
    presented, guarantee = load_pull_json(None)
    assert presented == "unavailable"
    assert guarantee is None


def test_load_pull_json_transcribes_decisions_and_guarantee(tmp_path: Path):
    payload = {
        "guarantee": "enumerated",
        "topics": [
            {"topic_id_raw": 1, "decisions": [{"id_raw": 3101}, {"id_raw": 3102}]},
            {"topic_id_raw": 2, "decisions": [{"id_raw": 3103}]},
        ],
    }
    f = tmp_path / "pull.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    presented, guarantee = load_pull_json(str(f))
    assert guarantee == "enumerated"
    assert presented == [
        {"type": "decision", "id": 3101},
        {"type": "decision", "id": 3102},
        {"type": "decision", "id": 3103},
    ]


def test_load_pull_json_dedupes_repeated_decision_ids(tmp_path: Path):
    payload = {
        "guarantee": "enumerated",
        "topics": [
            {"topic_id_raw": 1, "decisions": [{"id_raw": 3101}, {"id_raw": 3101}]},
        ],
    }
    f = tmp_path / "pull.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    presented, _ = load_pull_json(str(f))
    assert presented == [{"type": "decision", "id": 3101}]


def test_load_pull_json_ignores_legacy_id_key_and_warns(tmp_path: Path, capsys):
    """旧スキーマ(id/topic_id)のdecisionは`id_raw`が無いため無視される(常にNoneになる不具合の再発防止)が、
    黙って捨てず標準エラー出力に警告を出す。"""
    payload = {
        "guarantee": "enumerated",
        "topics": [
            {"topic_id": 1, "decisions": [{"id": 3101}]},
        ],
    }
    f = tmp_path / "pull.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    presented, guarantee = load_pull_json(str(f))
    assert guarantee == "enumerated"
    assert presented == []
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "id_raw" in err


def test_load_pull_json_warns_when_id_raw_and_id_both_missing(tmp_path: Path, capsys):
    """id_raw/idどちらも無いdecision要素は転記をスキップしつつ、警告を出す。"""
    payload = {
        "guarantee": "enumerated",
        "topics": [
            {"topic_id_raw": 1, "decisions": [{}]},
        ],
    }
    f = tmp_path / "pull.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    presented, guarantee = load_pull_json(str(f))
    assert guarantee == "enumerated"
    assert presented == []
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "id_raw" in err


# ---------------------------------------------------------------------------
# aggregate_shadow_report / compute_missing_packages_for_prs(純粋関数)
# ---------------------------------------------------------------------------


def _pkg(material_id: int, created_at: str, detector_sha: str, divergence: str | None, prs: list[int] | None = None) -> dict:
    block = {"gate": {"detector_sha256": detector_sha}, "prs": prs or []}
    if divergence is not None:
        block["shadow"] = {"human": "pre_go", "divergence": divergence, "divergence_reason": None}
    return {"material_id": material_id, "created_at": created_at, "block": block}


def test_aggregate_shadow_report_counts_divergences():
    packages = [
        _pkg(1, "2026-01-01", "sha-a", "none"),
        _pkg(2, "2026-01-02", "sha-a", "false_negative"),
        _pkg(3, "2026-01-03", "sha-a", "gray_case"),
        _pkg(4, "2026-01-04", "sha-a", None),  # shadow未記入(集計対象外)
    ]
    report = aggregate_shadow_report(packages)
    assert report["total_packages"] == 4
    assert report["reviewed_packages"] == 3
    assert report["divergence_counts"]["none"] == 1
    assert report["divergence_counts"]["false_negative"] == 1
    assert report["divergence_counts"]["gray_case"] == 1
    assert report["divergence_counts"]["false_positive"] == 0


def test_aggregate_shadow_report_streak_resets_on_false_negative():
    packages = [
        _pkg(1, "2026-01-01", "sha-a", "none"),
        _pkg(2, "2026-01-02", "sha-a", "false_negative"),
        _pkg(3, "2026-01-03", "sha-a", "none"),
        _pkg(4, "2026-01-04", "sha-a", "none"),
    ]
    report = aggregate_shadow_report(packages)
    # 最新のfalse_negative(2件目)以降、非FNが2件連続
    assert report["consecutive_no_false_negative_by_detector_sha"]["sha-a"] == 2


def test_aggregate_shadow_report_streak_is_per_detector_sha():
    packages = [
        _pkg(1, "2026-01-01", "sha-old", "false_negative"),
        _pkg(2, "2026-01-02", "sha-old", "none"),
        _pkg(3, "2026-01-03", "sha-new", "none"),
        _pkg(4, "2026-01-04", "sha-new", "none"),
    ]
    report = aggregate_shadow_report(packages)
    streaks = report["consecutive_no_false_negative_by_detector_sha"]
    assert streaks["sha-old"] == 1
    assert streaks["sha-new"] == 2


def test_compute_missing_packages_for_prs():
    packages = [_pkg(1, "2026-01-01", "sha-a", "none", prs=[100]), _pkg(2, "2026-01-02", "sha-a", "none", prs=[101])]
    missing = compute_missing_packages_for_prs(packages, [100, 101, 102, 103])
    assert missing == [102, 103]


# ---------------------------------------------------------------------------
# new: gate_check.sh 連携(実gitリポジトリでの統合テスト)
# ---------------------------------------------------------------------------


def test_run_gate_check_returns_post_veto_candidate_for_small_clean_change(gate_repos: Path):
    (gate_repos / "src").mkdir()
    (gate_repos / "src" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    (gate_repos / "tests").mkdir()
    (gate_repos / "tests" / "test_foo.py").write_text("def test_x(): assert True\n", encoding="utf-8")
    base = _commit_all(gate_repos, "base")
    (gate_repos / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    (gate_repos / "tests" / "test_foo.py").write_text("def test_x(): assert True  # updated\n", encoding="utf-8")
    head = _commit_all(gate_repos, "small change with tests")

    verdict, verdict_text, gate_render_md = run_gate_check(gate_repos, base, head)
    assert verdict["classification"] == "post_veto_candidate"
    assert json.loads(verdict_text) == verdict
    assert "ブラスト半径" in gate_render_md
    assert "revert容易性" in gate_render_md


def test_run_gate_check_invokes_gate_check_sh_exactly_once(gate_repos: Path, monkeypatch):
    """gate_check.sh は1回だけ実行される(verdict/renderの2回叩きに戻る回帰を検知する)。

    gate_check.sh は呼ばれるたびに git fetch + diff 解析を行うため、2回叩くと
    フェッチ・計算コストが二重化する(--format both による単一呼び出し最適化の回帰防止)。
    """
    import scripts.go_package as go_package_module

    (gate_repos / "src").mkdir()
    (gate_repos / "src" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    base = _commit_all(gate_repos, "base")
    (gate_repos / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    head = _commit_all(gate_repos, "small change")

    real_run = go_package_module.subprocess.run
    call_count = 0

    def counting_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(go_package_module.subprocess, "run", counting_run)
    run_gate_check(gate_repos, base, head)
    assert call_count == 1


def test_cmd_new_writes_valid_package_to_out_file(gate_repos: Path, tmp_path: Path):
    (gate_repos / "migrations").mkdir()
    (gate_repos / "migrations" / "0049_x.sql").write_text("CREATE TABLE foo (id INTEGER);\n", encoding="utf-8")
    base = _commit_all(gate_repos, "base")
    (gate_repos / "migrations" / "0050_y.sql").write_text("ALTER TABLE foo ADD COLUMN bar TEXT;\n", encoding="utf-8")
    head = _commit_all(gate_repos, "add migration")

    out_path = tmp_path / "pkg.md"
    rc = main(
        [
            "new",
            "--activity",
            "99",
            "--base",
            base,
            "--head",
            head,
            "--repo",
            str(gate_repos),
            "--out",
            str(out_path),
        ]
    )
    assert rc == 0
    text = out_path.read_text(encoding="utf-8")
    data, err = extract_machine_block(text)
    assert err is None
    assert data["activity"] == 99
    assert data["gate"]["machine"] == "pre_go"  # migration接触はaxis_a_hit
    assert data["gate"]["effective"] == "pre_go"
    assert data["gate"]["detector_sha256"]
    assert data["gate"]["verdict_sha256"]

    result = lint_document(text, mode="shadow", allow_placeholder=True)
    assert result.errors == []


def test_cmd_new_with_pull_json_transcribes_presented(gate_repos: Path, tmp_path: Path):
    (gate_repos / "src").mkdir()
    (gate_repos / "src" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    base = _commit_all(gate_repos, "base")
    (gate_repos / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    head = _commit_all(gate_repos, "change")

    pull_json_path = tmp_path / "pull.json"
    pull_json_path.write_text(
        json.dumps({"guarantee": "enumerated", "topics": [{"topic_id_raw": 1, "decisions": [{"id_raw": 3101}]}]}),
        encoding="utf-8",
    )
    out_path = tmp_path / "pkg.md"
    rc = main(
        [
            "new",
            "--activity",
            "1",
            "--base",
            base,
            "--head",
            head,
            "--repo",
            str(gate_repos),
            "--pull-json",
            str(pull_json_path),
            "--out",
            str(out_path),
        ]
    )
    assert rc == 0
    data, _ = extract_machine_block(out_path.read_text(encoding="utf-8"))
    assert data["pull"]["presented"] == [{"type": "decision", "id": 3101}]
    assert data["pull"]["guarantee"] == "enumerated"


# ---------------------------------------------------------------------------
# shadow-report: DB連携(実サービス層 + temp_db フィクスチャ)
# ---------------------------------------------------------------------------


def _shadow_pkg_content(activity: int, machine: str, human: str, divergence: str, detector_sha: str) -> str:
    block = build_machine_block(
        activity=activity,
        machine_classification=machine,
        detector_sha256=detector_sha,
        verdict_sha256="v",
        predicted=None,
        presented="unavailable",
        guarantee=None,
    )
    block["shadow"] = {"human": human, "divergence": divergence, "divergence_reason": None}
    return _filled_doc(block)


def test_shadow_report_aggregates_materials_tagged_go_package(temp_db, capsys):
    from src.services.material_service import add_material

    add_material(
        title="GO: pkg1",
        content=_shadow_pkg_content(1, "post_veto_candidate", "post_veto_candidate", "none", "sha-a"),
        tags=["go-package", "domain:calm"],
        source="test",
    )
    add_material(
        title="GO: pkg2",
        content=_shadow_pkg_content(2, "post_veto_candidate", "pre_go", "false_negative", "sha-a"),
        tags=["go-package", "domain:calm"],
        source="test",
    )
    add_material(
        title="not a package",
        content="ただのメモです",
        tags=["domain:calm"],
        source="test",
    )

    rc = main(["shadow-report", "--db", temp_db])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_packages"] == 2
    assert report["reviewed_packages"] == 2
    assert report["divergence_counts"]["false_negative"] == 1
    assert report["divergence_counts"]["none"] == 1


def test_shadow_report_skips_materials_without_valid_machine_block(temp_db, capsys):
    from src.services.material_service import add_material

    add_material(
        title="broken go-package",
        content="```go-package\nkey: [unclosed\n```\n本文",
        tags=["go-package", "domain:calm"],
        source="test",
    )
    rc = main(["shadow-report", "--db", temp_db])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_packages"] == 0
    assert report["unparseable_packages"] == 1
