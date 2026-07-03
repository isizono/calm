"""migration / MCPツールIF変更と外縁ドキュメント更新の同一PR co-change lint。

git diffだけで判定できる規約をCIで強制する（.github/workflows/test.ymlから呼ばれる）:

1. migrations/*.sql に差分がある PR は docs/spec/db-schema.md にも差分があること。
   例外: コミットメッセージまたはPR本文に `[no-schema-shape-change]` を含める
   （index追加のみ等、スキーマ形状が変わらない変更）。
2. src/main.py の @mcp.tool() デコレータ付き関数のシグネチャ・増減に差分がある PR は
   docs/spec/mcp-tools.md にも差分があること。
   例外: `[no-tool-surface-change]` を含める。

判定不能（ast parse失敗等）は警告のみでpass する（doc lintで開発を止めない）。

使い方:
    uv run python scripts/lint_doc_cochange.py --base <ref> --head <ref>

PR本文をチェック対象に含めるには環境変数 CCM_PR_BODY にPR本文を渡す
（GitHub Actions では `${{ github.event.pull_request.body }}` を渡す想定）。
"""
import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

DB_SCHEMA_DOC = "docs/spec/db-schema.md"
MCP_TOOLS_DOC = "docs/spec/mcp-tools.md"
NO_SCHEMA_SHAPE_CHANGE_MARKER = "[no-schema-shape-change]"
NO_TOOL_SURFACE_CHANGE_MARKER = "[no-tool-surface-change]"

ToolSignature = dict[str, list[tuple[str, str | None, bool]]]


# ---------------------------------------------------------------------------
# git 連携（薄いラッパ。判定ロジック本体は純粋関数にして単体テストしやすくする）
# ---------------------------------------------------------------------------


def git_diff_names(repo_root: Path, base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def git_show(repo_root: Path, ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def collect_commit_messages(repo_root: Path, base: str, head: str) -> str:
    result = subprocess.run(
        ["git", "log", "--format=%B", f"{base}..{head}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else ""


# ---------------------------------------------------------------------------
# @mcp.tool() シグネチャ抽出（純粋関数、ast のみに依存）
# ---------------------------------------------------------------------------


def _has_mcp_tool_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            if isinstance(target.value, ast.Name) and target.value.id == "mcp":
                return True
    return False


def _annotation_str(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparseable>"


def _signature_shape(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, str | None, bool]]:
    args = node.args
    positional = list(args.posonlyargs) + list(args.args)
    defaults_count = len(args.defaults)
    default_offset = len(positional) - defaults_count

    shape: list[tuple[str, str | None, bool]] = []
    for i, a in enumerate(positional):
        has_default = i >= default_offset
        shape.append((a.arg, _annotation_str(a.annotation), has_default))

    for a, kw_default in zip(args.kwonlyargs, args.kw_defaults):
        shape.append((a.arg, _annotation_str(a.annotation), kw_default is not None))

    return shape


def extract_tool_signatures(source: str) -> ToolSignature | None:
    """@mcp.tool() 装飾された関数名 -> シグネチャ形状のdict。パース失敗時はNone。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    result: ToolSignature = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _has_mcp_tool_decorator(node):
            result[node.name] = _signature_shape(node)
    return result


def diff_tool_signatures(base: ToolSignature, head: ToolSignature) -> dict[str, list[str]]:
    """ツール名の増減 + 既存ツールのシグネチャ変更を検出する。差分無しは空dict。"""
    added = sorted(set(head) - set(base))
    removed = sorted(set(base) - set(head))
    changed = sorted(name for name in (set(base) & set(head)) if base[name] != head[name])

    diff: dict[str, list[str]] = {}
    if added:
        diff["added"] = added
    if removed:
        diff["removed"] = removed
    if changed:
        diff["changed"] = changed
    return diff


# ---------------------------------------------------------------------------
# 判定ロジック本体（純粋関数。git呼び出しから切り離してテストしやすくする）
# ---------------------------------------------------------------------------


def has_exception_marker(marker: str, commit_messages: str, pr_body: str) -> bool:
    return marker in commit_messages or marker in pr_body


def evaluate(
    changed_files: list[str],
    commit_messages: str,
    pr_body: str,
    base_main_py: str | None,
    head_main_py: str | None,
) -> tuple[list[str], list[str]]:
    """(failures, warnings) を返す。failuresが非空ならlintはexit 1で落ちる。"""
    failures: list[str] = []
    warnings: list[str] = []

    changed_set = set(changed_files)

    # 1. migrations/*.sql <-> db-schema.md
    migration_changed = any(
        f.startswith("migrations/") and f.endswith(".sql") for f in changed_files
    )
    if migration_changed and DB_SCHEMA_DOC not in changed_set:
        if has_exception_marker(NO_SCHEMA_SHAPE_CHANGE_MARKER, commit_messages, pr_body):
            pass
        else:
            failures.append(
                f"migrations/*.sql に差分があるが {DB_SCHEMA_DOC} に差分がない。"
                f"スキーマ形状が変わらない変更（index追加のみ等）なら "
                f"コミットメッセージまたはPR本文に {NO_SCHEMA_SHAPE_CHANGE_MARKER} を含めること。"
            )

    # 2. src/main.py の @mcp.tool() <-> mcp-tools.md
    if "src/main.py" in changed_set:
        if base_main_py is None or head_main_py is None:
            warnings.append("src/main.py の base/head 取得に失敗した。ツールIF差分の判定をスキップした。")
        else:
            base_sig = extract_tool_signatures(base_main_py)
            head_sig = extract_tool_signatures(head_main_py)
            if base_sig is None or head_sig is None:
                warnings.append("src/main.py の ast parse に失敗した。ツールIF差分の判定をスキップした。")
            else:
                diff = diff_tool_signatures(base_sig, head_sig)
                if diff and MCP_TOOLS_DOC not in changed_set:
                    if has_exception_marker(NO_TOOL_SURFACE_CHANGE_MARKER, commit_messages, pr_body):
                        pass
                    else:
                        failures.append(
                            f"@mcp.tool() のシグネチャ/増減に差分があるが {MCP_TOOLS_DOC} に差分がない "
                            f"(diff: {diff})。"
                            f"意図的な例外なら {NO_TOOL_SURFACE_CHANGE_MARKER} を含めること。"
                        )

    return failures, warnings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="比較元ref（例: origin/main）")
    parser.add_argument("--head", required=True, help="比較先ref（例: HEAD）")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    args = parser.parse_args(argv)

    changed_files = git_diff_names(args.repo_root, args.base, args.head)
    commit_messages = collect_commit_messages(args.repo_root, args.base, args.head)
    pr_body = os.environ.get("CCM_PR_BODY", "")

    base_main_py = None
    head_main_py = None
    if "src/main.py" in changed_files:
        base_main_py = git_show(args.repo_root, args.base, "src/main.py")
        head_main_py = git_show(args.repo_root, args.head, "src/main.py")

    failures, warnings = evaluate(
        changed_files, commit_messages, pr_body, base_main_py, head_main_py
    )

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1

    print("lint_doc_cochange: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
