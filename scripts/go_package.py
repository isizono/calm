#!/usr/bin/env python3
"""GO判定パッケージツール: テンプレート出力・雛形生成・lint・機械可読ブロック抽出・shadow集計。

1設計案 = 1パッケージ。markdown文書で、先頭に機械可読ブロック(```go-package
フェンス内YAML)、続いて人間が読む3区分本文(1-a 分類判定材料 / 1-b 地図メンテ材料 /
1-c 品質証跡)を置く。

サブコマンド:
    template       テンプレートmarkdownをstdoutへ出す(空欄のプレースホルダのみ)
    new            gate_check.sh(正規呼び出し経路)を実行し、機械判定欄を
                   充填した雛形を生成する。人間記述欄はプレースホルダのまま
    lint           機械可読ブロックの整合性・一方向性・shadow整合性を検証する
    extract        機械可読ブロックをJSONでstdoutへ出す
    shadow-report  素タグ go-package のmaterial群からshadow集計を出す(read-only)

`shadow-report` のみ `src.db` に依存する。他のサブコマンドは標準ライブラリ + pyyaml
のみで動く。テンプレート文字列は本ファイル内で単一ソース化しており、lintの
セクション定義もこれと同じ定数を参照する(テンプレートとlintの乖離防止)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.env_compat import env_restore, env_set, env_snapshot  # noqa: E402

SCHEMA_VERSION = 1
FENCE_LANG = "go-package"
GO_PACKAGE_TAG = "go-package"

CLASSIFICATIONS = ("pre_go", "gray", "post_veto_candidate")
STRICTNESS = {"pre_go": 2, "gray": 1, "post_veto_candidate": 0}
STANCE_VALUES = ("applied", "out_of_scope", "conflicting", "informational")
DIVERGENCE_VALUES = ("none", "false_negative", "false_positive", "gray_case")
HUMAN_VALUES = ("pre_go", "post_veto_candidate")

# ---------------------------------------------------------------------------
# 3区分本文のセクション定義(テンプレート・lint 共通の単一ソース)
# ---------------------------------------------------------------------------

SECTION_BLAST = "ブラスト半径(機械判定)"
SECTION_REVERT = "revert容易性(機械判定)"
SECTION_PRECEDENTS = "判例引用"
SECTION_NOVEL = "判例が無かった論点"
SECTION_INVARIANT = "invariant の変更・新設"
SECTION_BEHAVIOR = "挙動差(before → after)"
SECTION_DEPENDENCY = "依存関係の変化"
SECTION_MAP_UPDATE = "地図更新パラグラフ"
SECTION_TEST_GUARANTEE = "テストが保証する性質"
SECTION_FAILURE_MODE = "想定故障モードと復旧手段"

# 3-6-2 の1-a×4, 1-b×4, 1-c×2 = 10小見出し全部を必須とする。
# (設計書の「8セクション」という数値は直後の内訳 1-a×4/1-b×4/1-c×2=10 と
# 食い違うため、内訳を正として10小見出し全部を対象にした。PR本文に明記)
REQUIRED_SECTIONS = (
    SECTION_BLAST,
    SECTION_REVERT,
    SECTION_PRECEDENTS,
    SECTION_NOVEL,
    SECTION_INVARIANT,
    SECTION_BEHAVIOR,
    SECTION_DEPENDENCY,
    SECTION_MAP_UPDATE,
    SECTION_TEST_GUARANTEE,
    SECTION_FAILURE_MODE,
)

# 見出しテキストに含まれるべきキーワード(全角/半角括弧ゆらぎを吸収するため
# 括弧を含まない語で判定する)
_SECTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (SECTION_BLAST, ("ブラスト半径",)),
    (SECTION_REVERT, ("revert", "容易性")),
    (SECTION_PRECEDENTS, ("判例引用",)),
    (SECTION_NOVEL, ("判例が無かった論点",)),
    (SECTION_INVARIANT, ("invariant",)),
    (SECTION_BEHAVIOR, ("挙動差",)),
    (SECTION_DEPENDENCY, ("依存関係の変化",)),
    (SECTION_MAP_UPDATE, ("地図更新パラグラフ",)),
    (SECTION_TEST_GUARANTEE, ("テストが保証する性質",)),
    (SECTION_FAILURE_MODE, ("想定故障モードと復旧手段",)),
)

_GUIDE_COMMENTS = {
    SECTION_NOVEL: "<!-- ここだけが人間の本質的新規判断。空なら「なし」と明記(空欄禁止) -->",
    SECTION_INVARIANT: "<!-- 「検出器自己変更は事前go固定」のような、以後の設計が依存する不変条件 -->",
    SECTION_BEHAVIOR: "<!-- ファイル名の列挙ではなく振る舞いの差分。「◯◯すると△△だったのが□□になる」 -->",
    SECTION_DEPENDENCY: "<!-- 新たに依存する/されるコンポーネント。なければ「なし」 -->",
    SECTION_MAP_UPDATE: "<!-- 読後にユーザーの頭の中の設計地図がどう書き換わるべきか、1段落 -->",
    SECTION_TEST_GUARANTEE: "<!-- pass件数ではなく検証項目。「PRIMARY KEY重複拒否」の粒度 -->",
    SECTION_FAILURE_MODE: "<!-- 壊れ方の列挙と、それぞれ「どう検知し、どう戻すか」 -->",
}

_PLACEHOLDER_GATE_RENDER = (
    f"### {SECTION_BLAST}\n"
    "<!-- gate_check の出力をそのまま貼る。手で書かない -->\n"
    "\n"
    f"### {SECTION_REVERT}\n"
)

_PLACEHOLDER_TITLE = "{変更の短い名前}"


# ---------------------------------------------------------------------------
# 機械可読ブロックの組み立て
# ---------------------------------------------------------------------------


def build_machine_block(
    activity: Optional[int],
    machine_classification: Optional[str],
    detector_sha256: Optional[str],
    verdict_sha256: Optional[str],
    predicted: Optional[str],
    presented: Union[str, list],
    guarantee: Optional[str],
) -> dict:
    """機械可読ブロックのdictを組み立てる。

    人間記述欄(precedents/novel_points/gray_resolution/shadow)はプレースホルダ
    (空リスト/null)のまま返す。effectiveはmachineと同値で初期化する(エスカレー
    ションは人間が後から明示的に行う)。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "go-package",
        "activity": activity,
        "prs": [],
        "gate": {
            "predicted": predicted,
            "machine": machine_classification,
            "effective": machine_classification,
            "escalated_by": None,
            "verdict_sha256": verdict_sha256,
            "detector_sha256": detector_sha256,
        },
        "pull": {"presented": presented, "guarantee": guarantee},
        "gray_resolution": {"resolved_to": None, "basis": []},
        "precedents": [],
        "novel_points": [],
    }


def render_yaml_block(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False).rstrip("\n")


def _render_precedents_table(precedents: list[dict]) -> str:
    lines = ["| 判例 | 内容要約 | 適用判断 | 根拠 |", "|---|---|---|---|"]
    for p in precedents:
        ref = p.get("ref", "") if isinstance(p, dict) else ""
        stance = p.get("stance", "") if isinstance(p, dict) else ""
        lines.append(f"| {ref} |  | {stance} |  |")
    return "\n".join(lines)


def _body_markdown(gate_render_md: str, precedents: list[dict]) -> str:
    table = _render_precedents_table(precedents)
    return (
        "## 1-a 分類判定材料\n"
        "\n"
        f"{gate_render_md.rstrip()}\n"
        "\n"
        f"### {SECTION_PRECEDENTS}\n"
        "<!-- 機械可読ブロックのprecedentsと1対1。中身要約を本体に、idは補助 -->\n"
        f"{table}\n"
        "\n"
        f"### {SECTION_NOVEL}\n"
        f"{_GUIDE_COMMENTS[SECTION_NOVEL]}\n"
        "\n"
        "## 1-b 地図メンテ材料\n"
        "\n"
        f"### {SECTION_INVARIANT}\n"
        f"{_GUIDE_COMMENTS[SECTION_INVARIANT]}\n"
        "\n"
        f"### {SECTION_BEHAVIOR}\n"
        f"{_GUIDE_COMMENTS[SECTION_BEHAVIOR]}\n"
        "\n"
        f"### {SECTION_DEPENDENCY}\n"
        f"{_GUIDE_COMMENTS[SECTION_DEPENDENCY]}\n"
        "\n"
        f"### {SECTION_MAP_UPDATE}\n"
        f"{_GUIDE_COMMENTS[SECTION_MAP_UPDATE]}\n"
        "\n"
        "## 1-c 品質証跡\n"
        "\n"
        f"### {SECTION_TEST_GUARANTEE}\n"
        f"{_GUIDE_COMMENTS[SECTION_TEST_GUARANTEE]}\n"
        "\n"
        f"### {SECTION_FAILURE_MODE}\n"
        f"{_GUIDE_COMMENTS[SECTION_FAILURE_MODE]}\n"
    )


def render_document(machine_block: dict, gate_render_md: str, title: str = _PLACEHOLDER_TITLE) -> str:
    yaml_text = render_yaml_block(machine_block)
    body = _body_markdown(gate_render_md, machine_block.get("precedents") or [])
    return f"# GO判定: {title}\n\n```{FENCE_LANG}\n{yaml_text}\n```\n\n{body}"


# ---------------------------------------------------------------------------
# 機械可読ブロックの抽出
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```go-package\n(.*?)\n```", re.DOTALL)


def find_machine_block_yaml(text: str) -> Optional[str]:
    m = _FENCE_RE.search(text)
    return m.group(1) if m else None


def extract_machine_block(text: str) -> tuple[Optional[dict], Optional[str]]:
    """機械可読ブロックをパースする。(data, error_message) を返す。"""
    yaml_text = find_machine_block_yaml(text)
    if yaml_text is None:
        return None, "L1: go-package コードフェンスが見つからない"
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return None, f"L1: YAML parse エラー: {exc}"
    if not isinstance(data, dict):
        return None, "L1: 機械可読ブロックが object ではない"
    return data, None


def _body_after_fence(text: str) -> str:
    m = _FENCE_RE.search(text)
    if not m:
        return text
    return text[m.end():]


_HEADING_RE = re.compile(r"^(#{2,3})\s+(.*)$")


def _classify_heading(heading_text: str) -> Optional[str]:
    for canonical, keywords in _SECTION_KEYWORDS:
        if all(kw in heading_text for kw in keywords):
            return canonical
    return None


def parse_sections(body: str) -> dict[str, str]:
    """本文markdownを"### "見出し単位のセクション本文に分割する。

    未知の見出しは無視する(current を None に戻し、その下の行はどのセクションにも
    属さない扱いにする)。"## "(レベル2)見出しもcurrentをリセットする。
    """
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in body.split("\n"):
        m = _HEADING_RE.match(line.strip())
        if m:
            level = len(m.group(1))
            if level == 3:
                canonical = _classify_heading(m.group(2).strip())
                current = canonical
                if canonical is not None:
                    sections.setdefault(canonical, [])
            else:
                current = None
            continue
        if current is not None:
            sections[current].append(line)
    return {k: "\n".join(v) for k, v in sections.items()}


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _is_section_empty(content: str) -> bool:
    stripped = _HTML_COMMENT_RE.sub("", content).strip()
    return stripped == ""


def _extract_table_rows(section_content: str) -> list[list[str]]:
    """markdown表からヘッダ行・区切り行を除いたデータ行を返す。"""
    candidate_rows: list[list[str]] = []
    for raw_line in section_content.split("\n"):
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        inner = line.strip("|")
        if re.fullmatch(r"[\s:-]+(\|[\s:-]+)*", inner):
            continue  # 区切り行(|---|---|...)
        cells = [c.strip() for c in line.strip("|").split("|")]
        candidate_rows.append(cells)
    if not candidate_rows:
        return []
    return candidate_rows[1:]  # 先頭はヘッダ行


# ---------------------------------------------------------------------------
# shadow divergence 対応表(3-7)
# ---------------------------------------------------------------------------

_DIVERGENCE_MATCH_TABLE = {
    ("post_veto_candidate", "post_veto_candidate"): "none",
    ("pre_go", "pre_go"): "none",
    ("post_veto_candidate", "pre_go"): "false_negative",
    ("pre_go", "post_veto_candidate"): "false_positive",
}


def expected_divergence(machine: str, human: str) -> str:
    if machine == "gray":
        return "gray_case"
    return _DIVERGENCE_MATCH_TABLE.get((machine, human), "gray_case")


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


@dataclass
class LintResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0


def _normalize_ref(ref: object) -> str:
    """判例参照文字列を突合用に正規化する。

    `gray_resolution.basis[].{type,id}` を `"<type> <id>"` に組み立てた key と
    `precedents[].ref` を照合する際、表記ゆれ(大文字小文字・番号記号 `#`・区切りの
    `-` や連続空白)を吸収する。型語と id の境界は1空白へ畳み込むだけなので、
    番号記号や `-` 区切り・大文字始まりの書き方は `"decision 42"` と同一視されるが、
    `"decision 4"` と `"decision 42"` のような id 相違は区別される。
    """
    return re.sub(r"[\s#\-]+", " ", str(ref).strip().lower()).strip()


def _basis_all_cited(basis: list[dict], precedents: list[dict]) -> bool:
    cited_refs = {
        _normalize_ref(p.get("ref"))
        for p in precedents
        if isinstance(p, dict) and p.get("stance")
    }
    for b in basis:
        if not isinstance(b, dict):
            return False
        ref = _normalize_ref(f"{b.get('type')} {b.get('id')}")
        if ref not in cited_refs:
            return False
    return True


def lint_document(text: str, mode: str = "shadow", allow_placeholder: bool = False) -> LintResult:
    """go-packageドキュメントをlintする。

    allow_placeholder=True のとき、まだ人間の記入が済んでいないドラフト状態
    (L2のセクション非空チェック・L6のshadowブロック必須チェック)を許容する。
    構造的な整合性チェック(L1/L3/L4/L5/L7/L8)は allow_placeholder に関わらず行う。
    """
    errors: list[str] = []
    warnings: list[str] = []

    data, l1_err = extract_machine_block(text)
    if l1_err:
        errors.append(l1_err)
    elif data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"L1: 未知の schema_version: {data.get('schema_version')!r}")

    body = _body_after_fence(text)
    sections = parse_sections(body)
    for canonical in REQUIRED_SECTIONS:
        if canonical not in sections:
            errors.append(f"L2: セクションが見つからない: {canonical}")
        elif not allow_placeholder and _is_section_empty(sections[canonical]):
            errors.append(f"L2: セクションが空: {canonical}")

    if data is None:
        return LintResult(errors=errors, warnings=warnings)

    precedents = data.get("precedents")
    if not isinstance(precedents, list):
        errors.append("L3: precedents がリストではない")
        precedents = []
    else:
        for i, p in enumerate(precedents):
            stance = (p or {}).get("stance") if isinstance(p, dict) else None
            if stance not in STANCE_VALUES:
                errors.append(f"L3: precedents[{i}].stance が不正: {stance!r}")
    table_rows = _extract_table_rows(sections.get(SECTION_PRECEDENTS, ""))
    if len(table_rows) != len(precedents):
        errors.append(
            f"L3: 判例引用テーブルの行数({len(table_rows)})と precedents 件数({len(precedents)})が不一致"
        )

    if "novel_points" not in data:
        errors.append("L4: novel_points キーが存在しない")
    elif not isinstance(data["novel_points"], list):
        errors.append("L4: novel_points がリストではない")

    gate = data.get("gate") or {}
    machine = gate.get("machine")
    effective = gate.get("effective")
    if machine not in STRICTNESS:
        errors.append(f"L5: gate.machine が不正: {machine!r}")
    elif effective not in STRICTNESS:
        errors.append(f"L5: gate.effective が不正: {effective!r}")
    else:
        m_strength = STRICTNESS[machine]
        e_strength = STRICTNESS[effective]
        if e_strength < m_strength:
            gray_resolution = data.get("gray_resolution") or {}
            exception_ok = (
                machine == "gray"
                and effective == "post_veto_candidate"
                and gray_resolution.get("resolved_to") == "post_veto_candidate"
                and bool(gray_resolution.get("basis"))
                and _basis_all_cited(gray_resolution.get("basis") or [], precedents)
            )
            if not exception_ok:
                errors.append(
                    "L5: gate.effective は gate.machine 以上の強度でなければならない "
                    f"(machine={machine}, effective={effective})"
                )
        elif e_strength > m_strength:
            if not gate.get("escalated_by"):
                errors.append("L5: 厳格化した場合は gate.escalated_by が必須")

    predicted = gate.get("predicted")
    if predicted is not None and machine is not None and predicted != machine:
        warnings.append(f"L7: gate.predicted({predicted}) と gate.machine({machine}) が乖離している")

    if mode == "shadow":
        shadow = data.get("shadow")
        if not shadow:
            if not allow_placeholder:
                errors.append("L6: --mode shadow では shadow ブロックが必須")
        else:
            human = shadow.get("human")
            divergence = shadow.get("divergence")
            if human not in HUMAN_VALUES:
                errors.append(f"L6: shadow.human が不正: {human!r}")
            elif divergence not in DIVERGENCE_VALUES:
                errors.append(f"L6: shadow.divergence が不正: {divergence!r}")
            elif machine in STRICTNESS:
                expected = expected_divergence(machine, human)
                if expected != divergence:
                    errors.append(
                        "L6: divergence の導出が誤り: "
                        f"machine={machine} human={human} 期待={expected} 実際={divergence}"
                    )

    if mode == "live":
        pull = data.get("pull") or {}
        if pull.get("presented") == "unavailable":
            errors.append("L8: --mode live では pull.presented が unavailable のままはエラー")

    return LintResult(errors=errors, warnings=warnings)


# ---------------------------------------------------------------------------
# pull-json (pull_precedents 応答) からの機械転記
# ---------------------------------------------------------------------------


def load_pull_json(path: Optional[str]) -> tuple[Union[str, list], Optional[str]]:
    """pull_precedents 応答JSON(docs/spec/mcp-tools.md 2.32節 pull_precedentsのスキーマ)から
    pull.presented / pull.guarantee を機械転記する。

    decisionのIDはreadable_id変換済みの`id_raw`キーで返る(`id`ではない)ため、
    そちらを読む。未指定時は稼働前扱い(presented="unavailable", guarantee=None)。

    decision要素に`id_raw`が無い場合は転記をスキップするが、沈黙のfailureを避ける
    ため標準エラー出力に警告を出す(呼び出しは中断しない)。
    """
    if not path:
        return "unavailable", None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    guarantee = data.get("guarantee")
    presented: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for topic in data.get("topics") or []:
        for decision in topic.get("decisions") or []:
            did = decision.get("id_raw")
            if did is None:
                if "id" in decision:
                    sys.stderr.write(
                        f"WARNING: pull_precedents応答のdecision要素が旧キー`id`のみを持ち"
                        f"`id_raw`が無いため転記をスキップした: {decision!r}\n"
                    )
                else:
                    sys.stderr.write(
                        f"WARNING: pull_precedents応答のdecision要素に`id_raw`が無いため"
                        f"転記をスキップした: {decision!r}\n"
                    )
                continue
            key = ("decision", did)
            if key in seen:
                continue
            seen.add(key)
            presented.append({"type": "decision", "id": did})
    return presented, guarantee


# ---------------------------------------------------------------------------
# shadow-report 集計(純粋関数。DBアクセスは cmd_shadow_report 側で行う)
# ---------------------------------------------------------------------------


def aggregate_shadow_report(packages: list[dict]) -> dict:
    """[{"material_id":..., "created_at":..., "block": <machine_block dict>}, ...]
    (created_at 昇順)から集計dictを作る。

    divergence_counts は shadow ブロックを持つパッケージのみ集計する
    (mode=shadow での human 判定追記が済んだパッケージのみが対象)。
    """
    total = len(packages)
    divergence_counts = {v: 0 for v in DIVERGENCE_VALUES}
    reviewed = 0
    for p in packages:
        shadow = (p.get("block") or {}).get("shadow")
        if not shadow:
            continue
        div = shadow.get("divergence")
        if div in divergence_counts:
            reviewed += 1
            divergence_counts[div] += 1

    by_sha: dict[str, list[dict]] = {}
    for p in packages:
        sha = ((p.get("block") or {}).get("gate") or {}).get("detector_sha256") or ""
        by_sha.setdefault(sha, []).append(p)

    streaks: dict[str, int] = {}
    for sha, pkgs in by_sha.items():
        streak = 0
        for p in pkgs:  # 呼び出し側で created_at 昇順ソート済みの前提
            shadow = (p.get("block") or {}).get("shadow")
            if not shadow:
                continue
            if shadow.get("divergence") == "false_negative":
                streak = 0
            else:
                streak += 1
        streaks[sha] = streak

    return {
        "total_packages": total,
        "reviewed_packages": reviewed,
        "divergence_counts": divergence_counts,
        "consecutive_no_false_negative_by_detector_sha": streaks,
    }


def compute_missing_packages_for_prs(packages: list[dict], pr_numbers: list[int]) -> list[int]:
    covered: set[int] = set()
    for p in packages:
        for pr in (p.get("block") or {}).get("prs") or []:
            covered.add(pr)
    return sorted(set(pr_numbers) - covered)


# ---------------------------------------------------------------------------
# CLI: template
# ---------------------------------------------------------------------------


def cmd_template(_args: argparse.Namespace) -> int:
    machine_block = build_machine_block(
        activity=None,
        machine_classification="pre_go",
        detector_sha256=None,
        verdict_sha256=None,
        predicted=None,
        presented="unavailable",
        guarantee=None,
    )
    doc = render_document(machine_block, _PLACEHOLDER_GATE_RENDER)
    sys.stdout.write(doc if doc.endswith("\n") else doc + "\n")
    return 0


# ---------------------------------------------------------------------------
# CLI: new
# ---------------------------------------------------------------------------


def run_gate_check(repo: Path, base: str, head: str) -> tuple[dict, str, str]:
    """gate_check.sh(正規呼び出し経路)を実行し、(verdict, verdict_text, gate_render_md) を返す。

    origin/main 版の検出器で判定される(改竄耐性)。origin/main に検出器が
    未マージのときは worktree 版にフォールバックする(gate_check.sh 自身の挙動)。

    verdict JSON と markdown レンダリングは `--format both` の1回の呼び出しで
    まとめて取得する。gate_check.sh は呼ばれるたびに `git fetch origin main` と
    diff 解析を行うため、json と render を分けて2回叩くとフェッチ・計算が二重化する。
    both 出力は `<verdict_to_json>\n\n<render_markdown>` の連結で、JSON 部は
    indent=2 のため内部に空行(`\n\n`)を持たない。よって最初の `\n\n` が両者の
    境界となり、verdict_text は `--format json` 単独出力とバイト同一になる
    (verdict_sha256 の互換性を保つ)。
    """
    gate_sh = str(Path(__file__).resolve().parent / "gate_check.sh")
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "gate_out.txt"
        proc = subprocess.run(
            ["sh", gate_sh, "--base", base, "--head", head, "--repo", str(repo), "--format", "both", "--out", str(out_path)],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"gate_check.sh failed (exit {proc.returncode}): {proc.stderr}")
        combined = out_path.read_text(encoding="utf-8")

    parts = combined.split("\n\n", 1)
    if len(parts) != 2:
        raise RuntimeError("gate_check.sh --format both の出力を verdict/render に分割できなかった")
    verdict_text, gate_render_md = parts
    verdict = json.loads(verdict_text)
    return verdict, verdict_text, gate_render_md


def cmd_new(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    try:
        verdict, verdict_text, gate_render_md = run_gate_check(repo, args.base, args.head)
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    presented, guarantee = load_pull_json(args.pull_json)
    verdict_sha256 = hashlib.sha256(verdict_text.encode("utf-8")).hexdigest()
    machine_block = build_machine_block(
        activity=args.activity,
        machine_classification=verdict.get("classification"),
        detector_sha256=verdict.get("detector_sha256"),
        verdict_sha256=verdict_sha256,
        predicted=args.predicted,
        presented=presented,
        guarantee=guarantee,
    )
    doc = render_document(machine_block, gate_render_md)
    if args.out:
        Path(args.out).write_text(doc, encoding="utf-8")
    else:
        sys.stdout.write(doc if doc.endswith("\n") else doc + "\n")
    return 0


# ---------------------------------------------------------------------------
# CLI: lint
# ---------------------------------------------------------------------------


def cmd_lint(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8")
    result = lint_document(text, mode=args.mode, allow_placeholder=args.allow_placeholder)
    for w in result.warnings:
        print(f"WARNING: {w}")
    for e in result.errors:
        print(f"ERROR: {e}")
    if not result.errors and not result.warnings:
        print("OK: no issues found")
    return result.exit_code


# ---------------------------------------------------------------------------
# CLI: extract
# ---------------------------------------------------------------------------


def cmd_extract(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8")
    data, err = extract_machine_block(text)
    if err:
        sys.stderr.write(f"{err}\n")
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# CLI: shadow-report
# ---------------------------------------------------------------------------


def cmd_shadow_report(args: argparse.Namespace) -> int:
    # DBパスの環境変数は呼び出し前の値へ必ず復元する(プロセス内の以後の src.db 呼び出し
    # -- 同一プロセス内の他テスト・他サブコマンド呼び出しを含む -- への env leak を防ぐ)。
    # 新名は旧名(CCM_ / CC_MEMORY_)からフォールバックで読まれるため、控えも復元も
    # 新旧すべての名前をまとめて扱う。
    previous_db_env = env_snapshot("CALM_DB_PATH")
    if args.db:
        env_set("CALM_DB_PATH", args.db)
    try:
        from src.db import get_connection  # 遅延import: このサブコマンドのみ src.db に依存する

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT m.id AS id, m.content AS content, m.created_at AS created_at "
                "FROM materials m "
                "JOIN material_tags mt ON mt.material_id = m.id "
                "JOIN tags t ON t.id = mt.tag_id "
                "WHERE t.namespace = '' AND t.name = ?",
                (GO_PACKAGE_TAG,),
            ).fetchall()
        finally:
            conn.close()
    finally:
        if args.db:
            env_restore(previous_db_env)

    packages: list[dict] = []
    skipped = 0
    for row in rows:
        r = dict(row)
        block, err = extract_machine_block(r["content"])
        if err or block is None:
            skipped += 1
            continue
        packages.append({"material_id": r["id"], "created_at": r["created_at"], "block": block})

    packages.sort(key=lambda p: (p["created_at"], p["material_id"]))

    report = aggregate_shadow_report(packages)
    report["unparseable_packages"] = skipped

    if args.prs_file:
        pr_numbers = json.loads(Path(args.prs_file).read_text(encoding="utf-8"))
        report["missing_packages_for_prs"] = compute_missing_packages_for_prs(packages, pr_numbers)

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GO判定パッケージツール")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("template", help="テンプレートmarkdownをstdoutへ出す")

    p_new = sub.add_parser("new", help="機械判定欄を充填した雛形を生成する")
    p_new.add_argument("--activity", type=int, required=True, help="cc-memory activity id")
    p_new.add_argument("--base", default="origin/main")
    p_new.add_argument("--head", default="HEAD")
    p_new.add_argument("--predicted", choices=CLASSIFICATIONS, default=None)
    p_new.add_argument("--pull-json", default=None, help="pull_precedents 応答JSONファイル")
    p_new.add_argument("--out", default=None, help="出力先パス(省略時はstdout)")
    p_new.add_argument("--repo", default=".", help="gitリポジトリのパス(既定: カレントディレクトリ)")

    p_lint = sub.add_parser("lint", help="go-packageドキュメントをlintする")
    p_lint.add_argument("file")
    p_lint.add_argument("--mode", choices=["shadow", "live"], default="shadow")
    p_lint.add_argument(
        "--allow-placeholder",
        action="store_true",
        help="人間記述欄が未記入のドラフト状態を許容する(L2非空チェック・L6shadow必須チェックのみ緩和)",
    )

    p_extract = sub.add_parser("extract", help="機械可読ブロックをJSONで出す")
    p_extract.add_argument("file")

    p_shadow = sub.add_parser("shadow-report", help="shadow集計を出す(read-only)")
    p_shadow.add_argument("--db", default=None, help="DBパスを上書きする(省略時は既定DB)")
    p_shadow.add_argument("--prs-file", default=None, help="merge済みPR番号一覧のJSONファイル(カバレッジ突合用)")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "template":
        return cmd_template(args)
    if args.command == "new":
        return cmd_new(args)
    if args.command == "lint":
        return cmd_lint(args)
    if args.command == "extract":
        return cmd_extract(args)
    if args.command == "shadow-report":
        return cmd_shadow_report(args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
