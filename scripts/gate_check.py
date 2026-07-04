#!/usr/bin/env python3
"""境界ゲート検出器: git diff からブラスト半径(軸A)とrevert容易性(軸B)を機械判定する。

標準ライブラリのみで動く単一ファイル。外部依存なしで `python3 scripts/gate_check.py`
として直接実行できる(uv sync 不要)。

判定不能なときは常に安全側(pre_go)へフォールバックする。検出器自身が例外で
落ちることは無く、どの経路でも verdict を返す(内部バグによる例外のみ非0終了)。

出力は decision(pre_go/gray/post_veto_candidate)と reason コードを持つ JSON
(verdict)。`--render` で verdict JSON を人間可読な markdown に変換できる。

正規の呼び出し経路は `scripts/gate_check.sh`(origin/main 版検出器を取り出して
実行する)であり、このファイル単体を worktree 上で直接叩いた判定は改竄され得る
参考値として扱うこと。
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import json
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Union

# ---------------------------------------------------------------------------
# 定数: 検出パターン・パスリスト・閾値
#
# migration 安全装置(破壊的変更 lint)・PR サイズ lint はこのモジュールの
# パターン定数・閾値定数・count_diff_size() を単一ソースとして import する。
# ---------------------------------------------------------------------------

DDL_PATTERNS: tuple[str, ...] = (
    # 動詞と対象キーワードの間に UNIQUE / TEMP / TEMPORARY / VIRTUAL などの修飾語を
    # 挟む形(CREATE UNIQUE INDEX / CREATE TEMP TABLE / CREATE VIRTUAL TABLE 等)も拾う。
    r"(?i)\b(CREATE|ALTER|DROP)\s+(?:(?:UNIQUE|TEMP|TEMPORARY|VIRTUAL)\s+)*(TABLE|INDEX|TRIGGER|VIEW)\b",
    r"(?i)\bPRAGMA\s+\w+\s*=",  # PRAGMA の「書き込み」のみ。読み取りは対象外
)

DESTRUCTIVE_SQL_PATTERNS: tuple[str, ...] = (
    r"(?i)\bDELETE\s+FROM\b",
    r"(?i)\bUPDATE\s+\w+\s+SET\b",  # update_material( 等の関数名にはマッチしない
    r"(?i)\bTRUNCATE\b",
)

DESTRUCTIVE_FS_PATTERNS: tuple[str, ...] = (
    r"\bshutil\.rmtree\b",
    r"\bos\.(remove|unlink|rmdir)\b",
    r"\.unlink\(",
)

PUBLIC_IF_PATHS: tuple[str, ...] = (
    "src/main.py",
    "src/remote.py",
    "src/http_config.py",
    "src/services/visibility_middleware.py",
    "docs/spec/openapi.yaml",
    "hooks/hooks.json",
    "marketplace.json",
)

DEPENDENCY_PATHS: tuple[str, ...] = (
    "pyproject.toml",
    "uv.lock",
)

# 判定器を触る変更が判定を迂回できないための自己保護パス。
# ここに列挙されたパスへの接触は他の検出結果によらず pre_go に固定する。
DETECTOR_SELF_PATHS: tuple[str, ...] = (
    "scripts/gate_check.py",
    "scripts/gate_check.sh",
    "scripts/go_package.py",
    ".github/workflows/gate.yml",
    "tests/unit/test_gate_check.py",
    "tests/unit/test_go_package.py",
)

DEPENDENCY_LOCK_FILE = "uv.lock"

MAX_LINES = 400
MAX_FILES = 15

EVIDENCE_MAX_LEN = 200

Classification = Literal["pre_go", "gray", "post_veto_candidate"]


# ---------------------------------------------------------------------------
# 中間表現
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileChange:
    """git diff --name-status 1件分の正規化表現。"""

    path: str  # rename は新パス
    old_path: Optional[str]
    status: str  # "A" | "M" | "D" | "R" | "C" | "T"
    additions: int  # numstat 由来。バイナリは -1、未解決は 0
    deletions: int
    is_binary: bool


@dataclass(frozen=True)
class NumstatRow:
    """git diff --numstat 1行分の正規化表現。"""

    path: str
    old_path: Optional[str]
    additions: int  # バイナリは -1
    deletions: int
    is_binary: bool


@dataclass(frozen=True)
class DiffLine:
    """git diff --unified=0 の追加/削除行1件。"""

    path: str
    sign: str  # "+" | "-"
    new_lineno: Optional[int]  # 追加行のみ
    old_lineno: Optional[int]  # 削除行のみ
    text: str  # +/- 記号を除いた本文


@dataclass(frozen=True)
class Finding:
    detector: str
    path: str
    lineno: Optional[int]
    evidence: str
    status: str  # "counted" | "downgraded_tests" | "policy_pending"


@dataclass(frozen=True)
class AxisB:
    lines_changed: int
    files_changed: int
    size_ok: bool
    has_tests: Union[bool, str]  # True / False / "waived_docs_only"
    mechanical_rollback: bool
    met: bool


_EMPTY_PUBLIC_IF_DELTA = {
    "tools_added": [],
    "tools_removed": [],
    "params_changed": [],
    "docstring_changed": [],
}


# ---------------------------------------------------------------------------
# git 呼び出し
# ---------------------------------------------------------------------------


def _run_git_bytes(repo: Path, args: list[str]) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


def _run_git_text(repo: Path, args: list[str]) -> str:
    return _run_git_bytes(repo, args).decode("utf-8")


def get_merge_base(repo: Path, base_ref: str, head_ref: str) -> str:
    return _run_git_text(repo, ["merge-base", base_ref, head_ref]).strip()


def get_head_sha(repo: Path, head_ref: str) -> str:
    return _run_git_text(repo, ["rev-parse", head_ref]).strip()


def _git_show_file(repo: Path, rev: str, path: str) -> Optional[str]:
    """rev 時点の path の内容を返す。存在しない(新規追加/既に削除)なら None。"""
    try:
        raw = _run_git_bytes(repo, ["show", f"{rev}:{path}"])
    except subprocess.CalledProcessError:
        return None
    return raw.decode("utf-8")


# ---------------------------------------------------------------------------
# パース
# ---------------------------------------------------------------------------


def parse_name_status(raw: bytes) -> list[FileChange]:
    """`git diff --name-status -M -z` の出力をパースする。"""
    text = raw.decode("utf-8")
    tokens = text.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    changes: list[FileChange] = []
    i = 0
    while i < len(tokens):
        status_field = tokens[i]
        if not status_field:
            raise ValueError("empty status field in name-status output")
        status = status_field[0]
        i += 1
        if status in ("R", "C"):
            if i + 1 >= len(tokens):
                raise ValueError(f"truncated rename/copy record: {status_field!r}")
            old_path = tokens[i]
            i += 1
            new_path = tokens[i]
            i += 1
            changes.append(
                FileChange(path=new_path, old_path=old_path, status=status, additions=0, deletions=0, is_binary=False)
            )
        else:
            if i >= len(tokens):
                raise ValueError(f"truncated record: {status_field!r}")
            path = tokens[i]
            i += 1
            changes.append(
                FileChange(path=path, old_path=None, status=status, additions=0, deletions=0, is_binary=False)
            )
    return changes


def parse_numstat(raw: bytes) -> list[NumstatRow]:
    """`git diff --numstat -M -z` の出力をパースする。

    -z のため各レコードは NUL 区切り。通常の変更は `add<TAB>del<TAB>path` が
    1 要素で来るが、rename/copy は `add<TAB>del<TAB>`(path 欄が空)の後に
    old-path・new-path が別々の NUL 要素として続く。-z を付けることで
    name-status 側と同様に生パスを受け取れ、非ASCII・空白入りパスの
    quotepath エスケープ差異を避けられる。
    """
    text = raw.decode("utf-8")
    tokens = text.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    rows: list[NumstatRow] = []
    i = 0
    while i < len(tokens):
        head = tokens[i]
        i += 1
        parts = head.split("\t")
        if len(parts) != 3:
            raise ValueError(f"unparseable numstat record: {head!r}")
        added_s, deleted_s, pathspec = parts
        is_binary = added_s == "-" or deleted_s == "-"
        additions = -1 if is_binary else int(added_s)
        deletions = -1 if is_binary else int(deleted_s)
        if pathspec == "":
            # rename/copy: old-path・new-path が後続の NUL 要素として続く
            if i + 1 >= len(tokens):
                raise ValueError(f"truncated numstat rename record: {head!r}")
            old_path: Optional[str] = tokens[i]
            i += 1
            new_path = tokens[i]
            i += 1
        else:
            old_path = None
            new_path = pathspec
        rows.append(NumstatRow(path=new_path, old_path=old_path, additions=additions, deletions=deletions, is_binary=is_binary))
    return rows


def merge_numstat_into_changes(changes: list[FileChange], numstat_rows: list[NumstatRow]) -> list[FileChange]:
    """numstat の additions/deletions/is_binary を name-status 由来の FileChange に反映する。"""
    by_path = {row.path: row for row in numstat_rows}
    merged: list[FileChange] = []
    for c in changes:
        row = by_path.get(c.path)
        if row is not None:
            merged.append(dataclasses.replace(c, additions=row.additions, deletions=row.deletions, is_binary=row.is_binary))
        else:
            merged.append(c)
    return merged


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_diff_prefix(path: str) -> Optional[str]:
    if path == "/dev/null":
        return None
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def parse_diff_lines(diff_text: str) -> list[DiffLine]:
    """`git diff --unified=0 --no-color` の出力から追加/削除行を抽出する。

    文脈行が無い(unified=0)ため、パーサは +/- 行だけを追えばよい。
    """
    lines = diff_text.split("\n")
    result: list[DiffLine] = []
    old_path: Optional[str] = None
    new_path: Optional[str] = None
    current_path: Optional[str] = None
    old_lineno: Optional[int] = None
    new_lineno: Optional[int] = None
    in_hunk = False

    for line in lines:
        if line.startswith("diff --git "):
            old_path = None
            new_path = None
            current_path = None
            in_hunk = False
            continue
        if line.startswith("--- "):
            old_path = _strip_diff_prefix(line[4:])
            continue
        if line.startswith("+++ "):
            new_path = _strip_diff_prefix(line[4:])
            current_path = new_path if new_path is not None else old_path
            in_hunk = False
            continue
        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if not m:
                raise ValueError(f"unparseable hunk header: {line!r}")
            old_lineno = int(m.group(1))
            new_lineno = int(m.group(3))
            in_hunk = True
            continue
        if not in_hunk or current_path is None:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            result.append(DiffLine(path=current_path, sign="+", new_lineno=new_lineno, old_lineno=None, text=line[1:]))
            new_lineno = (new_lineno or 0) + 1
        elif line.startswith("-"):
            result.append(DiffLine(path=current_path, sign="-", new_lineno=None, old_lineno=old_lineno, text=line[1:]))
            old_lineno = (old_lineno or 0) + 1
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            continue
    return result


def _evidence(sign: str, text: str, max_len: int = EVIDENCE_MAX_LEN) -> str:
    joined = f"{sign}{text}"
    return joined[:max_len]


# ---------------------------------------------------------------------------
# 軸A検出器
# ---------------------------------------------------------------------------


def _is_docs_excluded(path: str) -> bool:
    return path.endswith(".md") or path.startswith("docs/")


def _is_tests_path(path: str) -> bool:
    return path.startswith("tests/")


def detect_migration_touch(changes: list[FileChange]) -> list[Finding]:
    findings = []
    for c in changes:
        touched_paths = [c.path] + ([c.old_path] if c.old_path else [])
        if any(p.startswith("migrations/") for p in touched_paths):
            findings.append(Finding(detector="migration_touch", path=c.path, lineno=None, evidence=f"status={c.status}", status="counted"))
    return findings


def detect_ddl_in_code(diff_lines: list[DiffLine]) -> list[Finding]:
    findings = []
    for dl in diff_lines:
        if _is_docs_excluded(dl.path):
            continue
        for pattern in DDL_PATTERNS:
            if re.search(pattern, dl.text):
                status = "downgraded_tests" if _is_tests_path(dl.path) else "counted"
                lineno = dl.new_lineno if dl.sign == "+" else dl.old_lineno
                findings.append(Finding(detector="ddl_in_code", path=dl.path, lineno=lineno, evidence=_evidence(dl.sign, dl.text), status=status))
                break
    return findings


def detect_public_if(changes: list[FileChange]) -> list[Finding]:
    findings = []
    public_if_set = set(PUBLIC_IF_PATHS)
    for c in changes:
        touched_paths = {c.path}
        if c.old_path:
            touched_paths.add(c.old_path)
        if touched_paths & public_if_set:
            findings.append(Finding(detector="public_if", path=c.path, lineno=None, evidence=f"status={c.status}", status="counted"))
    return findings


def detect_data_destructive(diff_lines: list[DiffLine]) -> list[Finding]:
    findings = []
    patterns = DESTRUCTIVE_SQL_PATTERNS + DESTRUCTIVE_FS_PATTERNS
    for dl in diff_lines:
        if dl.sign != "+":
            continue
        if _is_docs_excluded(dl.path):
            continue
        for pattern in patterns:
            if re.search(pattern, dl.text):
                status = "downgraded_tests" if _is_tests_path(dl.path) else "counted"
                findings.append(Finding(detector="data_destructive", path=dl.path, lineno=dl.new_lineno, evidence=_evidence(dl.sign, dl.text), status=status))
                break
    return findings


def detect_binary_change(changes: list[FileChange]) -> list[Finding]:
    return [
        Finding(detector="binary_change", path=c.path, lineno=None, evidence=f"status={c.status}", status="counted")
        for c in changes
        if c.is_binary
    ]


def detect_dependency_change(changes: list[FileChange]) -> list[Finding]:
    findings = []
    dep_set = set(DEPENDENCY_PATHS)
    for c in changes:
        touched_paths = {c.path}
        if c.old_path:
            touched_paths.add(c.old_path)
        if touched_paths & dep_set:
            findings.append(Finding(detector="dependency_change", path=c.path, lineno=None, evidence=f"status={c.status}", status="policy_pending"))
    return findings


def counted(findings: list[Finding]) -> bool:
    return any(f.status == "counted" for f in findings)


def pending(findings: list[Finding]) -> bool:
    return any(f.status == "policy_pending" for f in findings)


def is_self_touched(changes: list[FileChange]) -> bool:
    self_paths = set(DETECTOR_SELF_PATHS)
    for c in changes:
        if c.path in self_paths:
            return True
        if c.old_path and c.old_path in self_paths:
            return True
    return False


# ---------------------------------------------------------------------------
# 公開IF差分の AST 抽出(判定材料の充実。判定そのものには使わない)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSig:
    name: str
    params: tuple[str, ...]
    docstring_sha256: str


def _is_mcp_tool_decorator(dec: ast.expr) -> bool:
    node: ast.expr = dec.func if isinstance(dec, ast.Call) else dec
    return isinstance(node, ast.Attribute) and node.attr == "tool" and isinstance(node.value, ast.Name) and node.value.id == "mcp"


def _unparse_safe(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<?>"


def _format_tool_params(node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> tuple[str, ...]:
    args = node.args
    params: list[str] = []
    positional = list(getattr(args, "posonlyargs", [])) + list(args.args)
    defaults = list(args.defaults)
    num_no_default = len(positional) - len(defaults)
    for idx, a in enumerate(positional):
        seg = a.arg
        if a.annotation is not None:
            seg += f": {_unparse_safe(a.annotation)}"
        if idx >= num_no_default:
            seg += f" = {_unparse_safe(defaults[idx - num_no_default])}"
        params.append(seg)
    if args.vararg:
        seg = f"*{args.vararg.arg}"
        if args.vararg.annotation is not None:
            seg += f": {_unparse_safe(args.vararg.annotation)}"
        params.append(seg)
    for kwa, d in zip(args.kwonlyargs, args.kw_defaults):
        seg = kwa.arg
        if kwa.annotation is not None:
            seg += f": {_unparse_safe(kwa.annotation)}"
        if d is not None:
            seg += f" = {_unparse_safe(d)}"
        params.append(seg)
    if args.kwarg:
        seg = f"**{args.kwarg.arg}"
        if args.kwarg.annotation is not None:
            seg += f": {_unparse_safe(args.kwarg.annotation)}"
        params.append(seg)
    return tuple(params)


def extract_tool_surface(source: Optional[str], errors: list[str], label: str) -> dict[str, ToolSig]:
    """`@mcp.tool()` 付き関数の表面を抽出する。

    parse 失敗は例外を投げず errors に積んで空辞書を返す(呼び出し側の
    public_if パス hit が既に判定へ影響しているため、ここでの失敗は
    判定材料の欠落としてのみ扱う)。
    """
    if source is None:
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        errors.append(f"ast_parse_failed:{label}: {exc}")
        return {}
    tools: dict[str, ToolSig] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_is_mcp_tool_decorator(dec) for dec in node.decorator_list):
                docstring = ast.get_docstring(node) or ""
                tools[node.name] = ToolSig(
                    name=node.name,
                    params=_format_tool_params(node),
                    docstring_sha256=hashlib.sha256(docstring.encode("utf-8")).hexdigest(),
                )
    return tools


def _param_name(param: str) -> str:
    return param.lstrip("*").split(":")[0].split("=")[0].strip()


def _describe_params_change(name: str, before: tuple[str, ...], after: tuple[str, ...]) -> str:
    before_names = {_param_name(p) for p in before}
    after_names = {_param_name(p) for p in after}
    added = sorted(after_names - before_names)
    removed = sorted(before_names - after_names)
    before_by_name = {_param_name(p): p for p in before}
    after_by_name = {_param_name(p): p for p in after}
    changed = sorted(n for n in (before_names & after_names) if before_by_name[n] != after_by_name[n])
    parts = []
    if added:
        parts.append(f"{','.join(added)} 引数追加")
    if removed:
        parts.append(f"{','.join(removed)} 引数削除")
    if changed:
        parts.append(f"{','.join(changed)} 型/デフォルト変更")
    if not parts:
        parts.append("引数変更")
    return f"{name}: {'; '.join(parts)}"


def compute_public_if_delta(base_source: Optional[str], head_source: Optional[str], errors: list[str]) -> dict:
    base_tools = extract_tool_surface(base_source, errors, "base")
    head_tools = extract_tool_surface(head_source, errors, "head")
    added = sorted(set(head_tools) - set(base_tools))
    removed = sorted(set(base_tools) - set(head_tools))
    common = set(base_tools) & set(head_tools)
    params_changed = sorted(
        _describe_params_change(name, base_tools[name].params, head_tools[name].params)
        for name in common
        if base_tools[name].params != head_tools[name].params
    )
    docstring_changed = sorted(name for name in common if base_tools[name].docstring_sha256 != head_tools[name].docstring_sha256)
    return {
        "tools_added": added,
        "tools_removed": removed,
        "params_changed": params_changed,
        "docstring_changed": docstring_changed,
    }


# ---------------------------------------------------------------------------
# 軸B検出器
# ---------------------------------------------------------------------------


def _is_core_code_path(path: str) -> bool:
    if path.startswith("tests/") or path.startswith("docs/"):
        return False
    if path.endswith(".md"):
        return False
    return True


def count_diff_size(numstat_rows: list[NumstatRow]) -> tuple[int, int]:
    """(lines_changed, files_changed) を返す。

    lines_changed の母集団は tests/・docs/・*.md・uv.lock を除外した本体コード行。
    files_changed は uv.lock のみ除外する。
    """
    lines = 0
    files = 0
    for row in numstat_rows:
        if row.path == DEPENDENCY_LOCK_FILE:
            continue
        files += 1
        if row.is_binary:
            continue
        if _is_core_code_path(row.path):
            lines += row.additions + row.deletions
    return lines, files


def compute_has_tests(numstat_rows: list[NumstatRow]) -> Union[bool, str]:
    paths = [row.path for row in numstat_rows if row.path != DEPENDENCY_LOCK_FILE]
    if not paths:
        return True
    if all(p.endswith(".md") or p.startswith("docs/") for p in paths):
        return "waived_docs_only"
    return any(p.startswith("tests/") for p in paths)


_MECHANICAL_ROLLBACK_DETECTORS = frozenset({"migration_touch", "ddl_in_code", "data_destructive", "binary_change"})


def compute_mechanical_rollback(findings: list[Finding]) -> bool:
    return not any(f.detector in _MECHANICAL_ROLLBACK_DETECTORS and f.status == "counted" for f in findings)


def compute_axis_b(numstat_rows: list[NumstatRow], findings: list[Finding]) -> AxisB:
    lines, files = count_diff_size(numstat_rows)
    size_ok = lines <= MAX_LINES and files <= MAX_FILES
    has_tests = compute_has_tests(numstat_rows)
    mechanical_rollback = compute_mechanical_rollback(findings)
    met = bool(size_ok and has_tests and mechanical_rollback)
    return AxisB(lines_changed=lines, files_changed=files, size_ok=size_ok, has_tests=has_tests, mechanical_rollback=mechanical_rollback, met=met)


# ---------------------------------------------------------------------------
# 判定規則
# ---------------------------------------------------------------------------


def classify(findings: list[Finding], axis_b: AxisB, errors: list[str], self_touched: bool) -> tuple[Classification, str]:
    """評価順序がフェイルセーフの実体。上から順に短絡する。"""
    if errors:
        return "pre_go", "detector_error"
    if self_touched:
        return "pre_go", "self_protection"
    if counted(findings):
        return "pre_go", "axis_a_hit"
    if pending(findings):
        return "gray", "policy_pending"
    if axis_b.met:
        return "post_veto_candidate", "axis_b_met"
    return "gray", "axis_b_unmet"


# ---------------------------------------------------------------------------
# verdict の組み立て
# ---------------------------------------------------------------------------


def _detector_sha256() -> str:
    try:
        data = Path(__file__).resolve().read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def _finding_to_dict(f: Finding) -> dict:
    return {"detector": f.detector, "path": f.path, "lineno": f.lineno, "evidence": f.evidence, "status": f.status}


def _finding_sort_key(f: Finding) -> tuple[str, str, int]:
    return (f.detector, f.path, -1 if f.lineno is None else f.lineno)


def run_detector(repo: Path, base_ref: str, head_ref: str, detector_source: str = "main") -> dict:
    """diff を取得・解析し、verdict(dict)を返す。

    どの経路でも例外を投げない(内部の全ステップを try/except で囲み、
    失敗は errors に積んで pre_go/detector_error にフォールバックする)。
    """
    errors: list[str] = []
    changes: list[FileChange] = []
    diff_lines: list[DiffLine] = []
    numstat_rows: list[NumstatRow] = []
    merge_base: Optional[str] = None
    head_sha: Optional[str] = None

    try:
        merge_base = get_merge_base(repo, base_ref, head_ref)
        head_sha = get_head_sha(repo, head_ref)
        name_status_raw = _run_git_bytes(repo, ["diff", "--name-status", "-M", "-z", merge_base, head_ref])
        changes = parse_name_status(name_status_raw)
        numstat_raw = _run_git_bytes(repo, ["diff", "--numstat", "-M", "-z", merge_base, head_ref])
        numstat_rows = parse_numstat(numstat_raw)
        changes = merge_numstat_into_changes(changes, numstat_rows)
        diff_text = _run_git_text(repo, ["diff", "--unified=0", "--no-color", "-M", merge_base, head_ref])
        diff_lines = parse_diff_lines(diff_text)
    except Exception as exc:  # noqa: BLE001 — F1: 判定不能は握り潰さず errors へ積む
        errors.append(f"{type(exc).__name__}: {exc}")

    findings: list[Finding] = []
    if not errors:
        try:
            findings = [
                *detect_migration_touch(changes),
                *detect_ddl_in_code(diff_lines),
                *detect_public_if(changes),
                *detect_data_destructive(diff_lines),
                *detect_binary_change(changes),
                *detect_dependency_change(changes),
            ]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    axis_b = AxisB(lines_changed=0, files_changed=0, size_ok=True, has_tests=True, mechanical_rollback=True, met=True)
    if not errors:
        try:
            axis_b = compute_axis_b(numstat_rows, findings)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    public_if_delta = dict(_EMPTY_PUBLIC_IF_DELTA)
    if not errors:
        try:
            touches_main = any(p in ("src/main.py",) for c in changes for p in (c.path, c.old_path) if p)
            if touches_main:
                base_source = _git_show_file(repo, merge_base, "src/main.py")
                head_source = _git_show_file(repo, head_ref, "src/main.py")
                public_if_delta = compute_public_if_delta(base_source, head_source, errors)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    self_touched = is_self_touched(changes)
    classification, reason = classify(findings, axis_b, errors, self_touched)

    sorted_findings = sorted(findings, key=_finding_sort_key)

    return {
        "schema_version": 1,
        "detector_sha256": _detector_sha256(),
        "detector_source": detector_source,
        "repo": repo.name,
        "base_ref": base_ref,
        "merge_base": merge_base or "",
        "head": head_sha or head_ref or "",
        "classification": classification,
        "reason": reason,
        "axis_a": {
            "hit": counted(findings) or pending(findings),
            "findings": [_finding_to_dict(f) for f in sorted_findings],
        },
        "axis_b": {
            "lines_changed": axis_b.lines_changed,
            "files_changed": axis_b.files_changed,
            "size_ok": axis_b.size_ok,
            "has_tests": axis_b.has_tests,
            "mechanical_rollback": axis_b.mechanical_rollback,
            "met": axis_b.met,
        },
        "public_if_delta": public_if_delta,
        "ignored_paths": [DEPENDENCY_LOCK_FILE],
        "errors": errors,
    }


def verdict_to_json(verdict: dict) -> str:
    """決定性のあるJSON文字列化(キー順固定)。"""
    return json.dumps(verdict, sort_keys=True, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# markdown レンダリング(1-a 用)
# ---------------------------------------------------------------------------


def render_markdown(verdict: dict) -> str:
    axis_a = verdict.get("axis_a") or {}
    axis_b = verdict.get("axis_b") or {}
    delta = verdict.get("public_if_delta") or {}
    findings = axis_a.get("findings") or []

    lines: list[str] = []
    lines.append("### ブラスト半径(機械判定)")
    lines.append(f"- 判定: {verdict.get('classification')}({verdict.get('reason')})")
    if findings:
        lines.append("- 検出:")
        for f in findings:
            loc = f"{f['path']}:{f['lineno']}" if f.get("lineno") is not None else f["path"]
            lines.append(f"  - `{f['detector']}` {loc} ({f['status']}): {f['evidence']}")
    else:
        lines.append("- 検出: 検出なし")

    delta_labels = (("tools_added", "追加"), ("tools_removed", "削除"), ("params_changed", "引数変更"), ("docstring_changed", "docstring変更"))
    delta_lines = [f"  - {label}: {', '.join(values)}" for key, label in delta_labels if (values := delta.get(key))]
    if delta_lines:
        lines.append("- 公開IF差分:")
        lines.extend(delta_lines)
    else:
        lines.append("- 公開IF差分: なし")

    lines.append("")
    lines.append("### revert容易性(機械判定)")
    lines.append(f"- 変更規模: {axis_b.get('lines_changed')}行 / {axis_b.get('files_changed')}ファイル(閾値 {MAX_LINES}/{MAX_FILES})")
    has_tests = axis_b.get("has_tests")
    if has_tests == "waived_docs_only":
        tests_desc = "docs-only免除"
    elif has_tests:
        tests_desc = "あり"
    else:
        tests_desc = "なし"
    lines.append(f"- テスト差分: {tests_desc}")
    rollback_desc = "成立" if axis_b.get("mechanical_rollback") else "不成立"
    lines.append(f"- 機械rollback: {rollback_desc}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="境界ゲート検出器: diff からブラスト半径とrevert容易性を機械判定する")
    parser.add_argument("--base", help="比較元 ref(例: origin/main)")
    parser.add_argument("--head", help="比較先 ref(例: HEAD)")
    parser.add_argument("--repo", default=".", help="git リポジトリのパス(既定: カレントディレクトリ)")
    parser.add_argument("--format", choices=["json", "markdown", "both"], default="json")
    parser.add_argument("--out", help="出力先パス(省略時は stdout)")
    parser.add_argument("--detector-source", choices=["main", "worktree"], default="main", help="verdict に記録するメタ情報")
    parser.add_argument("--render", metavar="VERDICT_JSON", help="verdict JSON ファイルを markdown に変換して出力する")
    return parser


def _write_output(text: str, out_path: Optional[str]) -> None:
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.render:
        verdict = json.loads(Path(args.render).read_text(encoding="utf-8"))
        _write_output(render_markdown(verdict), args.out)
        return 0

    if not args.base or not args.head:
        parser.error("--base と --head は --render 未指定時に必須です")

    repo = Path(args.repo).resolve()
    verdict = run_detector(repo, args.base, args.head, args.detector_source)

    if args.format == "json":
        output = verdict_to_json(verdict)
    elif args.format == "markdown":
        output = render_markdown(verdict)
    else:
        output = verdict_to_json(verdict) + "\n\n" + render_markdown(verdict)

    _write_output(output, args.out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — 内部例外のみ非0終了(仕様通り)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
