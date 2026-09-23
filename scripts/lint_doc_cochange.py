"""migration / MCPツールIF変更と外縁ドキュメント更新の同一PR co-change lint、
および README.md のツール・スキル表の実装との整合性チェック。

git diffだけで判定できる規約をCIで強制する（.github/workflows/test.ymlから呼ばれる）:

1. migrations/*.sql に差分がある PR は docs/spec/db-schema.md にも差分があること。
   例外: コミットメッセージまたはPR本文に `[no-schema-shape-change]` を含める
   （index追加のみ等、スキーマ形状が変わらない変更）。
2. src/main.py の @mcp.tool() デコレータ付き関数のシグネチャ・増減に差分がある PR は
   docs/spec/mcp-tools.md にも差分があること。
   例外: `[no-tool-surface-change]` を含める。
3. README.md の「MCPツール」表に載っているツール名の集合は、src/main.py の
   @mcp.tool() 登録関数の集合と一致すること（head ref の状態を毎回比較する。
   co-change判定ではないので例外マーカーは無い）。
4. README.md の「スキル」表に載っているスキル名の集合は、skills/*/SKILL.md が
   存在するディレクトリ名の集合と一致すること（同上、例外マーカーは無い）。

判定不能（ast parse失敗、対象セクションが見つからない等）は警告のみでpass する
（doc lintで開発を止めない）。

使い方:
    uv run python scripts/lint_doc_cochange.py --base <ref> --head <ref>

PR本文をチェック対象に含めるには環境変数 CALM_PR_BODY にPR本文を渡す
（GitHub Actions では `${{ github.event.pull_request.body }}` を渡す想定）。
"""
import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.env_compat import env_get  # noqa: E402

DB_SCHEMA_DOC = "docs/spec/db-schema.md"
MCP_TOOLS_DOC = "docs/spec/mcp-tools.md"
NO_SCHEMA_SHAPE_CHANGE_MARKER = "[no-schema-shape-change]"
NO_TOOL_SURFACE_CHANGE_MARKER = "[no-tool-surface-change]"

README_PATH = "README.md"
README_TOOLS_HEADING = "## MCPツール"
README_SKILLS_HEADING = "## スキル"

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


def git_ls_tree_paths(repo_root: Path, ref: str, dir_path: str) -> list[str] | None:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, "--", dir_path],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


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
# README.md 表パース（純粋関数。git呼び出しから切り離してテストしやすくする）
# ---------------------------------------------------------------------------


def _extract_section(text: str, heading: str) -> str | None:
    """指定見出し行の直後から次の `## ` 見出し（無ければ末尾）までを返す。見出しが無ければNone。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == heading:
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start:end])


def extract_readme_tool_names(readme_text: str) -> set[str] | None:
    """「MCPツール」表の「ツール」列（2列目）の backtick 名だけを集める。説明列は見ない。"""
    section = _extract_section(readme_text, README_TOOLS_HEADING)
    if section is None:
        return None
    names: set[str] = set()
    for line in section.splitlines():
        if not line.strip().startswith("|"):
            continue
        cols = line.split("|")
        if len(cols) < 4:
            continue
        names |= set(re.findall(r"`([^`]+)`", cols[2]))
    return names


def extract_readme_skill_names(readme_text: str) -> set[str] | None:
    """「スキル」表の1列目の backtick 名（先頭の `/` を除く）だけを集める。説明列は見ない。"""
    section = _extract_section(readme_text, README_SKILLS_HEADING)
    if section is None:
        return None
    names: set[str] = set()
    for line in section.splitlines():
        if not line.strip().startswith("|"):
            continue
        cols = line.split("|")
        if len(cols) < 3:
            continue
        names |= {n.lstrip("/") for n in re.findall(r"`([^`]+)`", cols[1])}
    return names


def extract_skill_dir_names(skill_paths: list[str]) -> set[str]:
    """`git ls-tree -r skills/` のパス一覧から SKILL.md を持つディレクトリ名を集める。"""
    names: set[str] = set()
    for p in skill_paths:
        parts = p.split("/")
        if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
            names.add(parts[1])
    return names


def check_readme_tables(
    readme_text: str | None,
    tool_names: set[str] | None,
    skill_names: set[str] | None,
) -> tuple[list[str], list[str]]:
    """README.md のMCPツール表・スキル表が実装と一致するかを判定する。co-changeではなく
    head refの状態同士を毎回突き合わせるスナップショット比較なので、例外マーカーは無い。"""
    failures: list[str] = []
    warnings: list[str] = []

    if readme_text is None:
        warnings.append(f"{README_PATH} の取得に失敗した。README表の突合をスキップした。")
        return failures, warnings

    readme_tool_names = extract_readme_tool_names(readme_text)
    if readme_tool_names is None:
        warnings.append(f"{README_PATH} に '{README_TOOLS_HEADING}' セクションが見つからない。MCPツール表の突合をスキップした。")
    elif tool_names is None:
        warnings.append("src/main.py からのツール名取得に失敗した。MCPツール表の突合をスキップした。")
    else:
        missing_in_readme = sorted(tool_names - readme_tool_names)
        extra_in_readme = sorted(readme_tool_names - tool_names)
        if missing_in_readme or extra_in_readme:
            failures.append(
                f"{README_PATH} の「MCPツール」表が実装とずれている "
                f"(README に無い: {missing_in_readme} / 実装に無い: {extra_in_readme})。"
                "ツールの追加・削除に合わせて表を更新すること。"
            )

    readme_skill_names = extract_readme_skill_names(readme_text)
    if readme_skill_names is None:
        warnings.append(f"{README_PATH} に '{README_SKILLS_HEADING}' セクションが見つからない。スキル表の突合をスキップした。")
    elif skill_names is None:
        warnings.append("skills/ 配下からのスキル名取得に失敗した。スキル表の突合をスキップした。")
    else:
        missing_in_readme = sorted(skill_names - readme_skill_names)
        extra_in_readme = sorted(readme_skill_names - skill_names)
        if missing_in_readme or extra_in_readme:
            failures.append(
                f"{README_PATH} の「スキル」表が実装とずれている "
                f"(README に無い: {missing_in_readme} / 実装に無い: {extra_in_readme})。"
                "スキルの追加・削除に合わせて表を更新すること。"
            )

    return failures, warnings


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
    pr_body = env_get("CALM_PR_BODY", "")

    base_main_py = None
    head_main_py = None
    if "src/main.py" in changed_files:
        base_main_py = git_show(args.repo_root, args.base, "src/main.py")
        head_main_py = git_show(args.repo_root, args.head, "src/main.py")

    failures, warnings = evaluate(
        changed_files, commit_messages, pr_body, base_main_py, head_main_py
    )

    # 3・4. README.md のMCPツール表・スキル表 <-> 実装（head refのスナップショット比較。
    # co-changeではないので常に実行する。差分が無いPRでも既存のドリフトを検出する）
    head_main_py_for_readme = head_main_py if head_main_py is not None else git_show(
        args.repo_root, args.head, "src/main.py"
    )
    tool_names = None
    if head_main_py_for_readme is not None:
        head_sig = extract_tool_signatures(head_main_py_for_readme)
        if head_sig is not None:
            tool_names = set(head_sig)

    skill_paths = git_ls_tree_paths(args.repo_root, args.head, "skills/")
    skill_names = extract_skill_dir_names(skill_paths) if skill_paths is not None else None

    head_readme = git_show(args.repo_root, args.head, README_PATH)
    readme_failures, readme_warnings = check_readme_tables(head_readme, tool_names, skill_names)
    failures += readme_failures
    warnings += readme_warnings

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
