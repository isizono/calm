#!/usr/bin/env python3
"""テストピラミッド網羅チェック: `tests/` 直下の全サブディレクトリが
`.github/workflows/*.yml` のいずれかのjobから参照されているかを検証する。

参照判定は「workflow YAMLの生テキストに `tests/<サブディレクトリ名>` という
文字列が含まれるか」の単純な文字列マッチで行う。pytestの実行パス引数・
`--collect-only` の対象パスいずれの書き方でも検出できる一方、ディレクトリ名が
たまたま別の文字列（コメント等）に含まれるだけでも誤って「参照あり」判定になる
点はこの実装のトレードオフ。

`tests/` 直下のサブディレクトリは動的に列挙するため、新しいテストディレクトリ
（`tests/foo/` 等）を追加してもこのスクリプト自体の変更は不要で、workflow側に
参照を追加し忘れるとCIがこのチェックで落ちる。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent


def discover_test_dirs(tests_root: Path) -> list[str]:
    """`tests/` 直下のサブディレクトリ名一覧を返す(隠しディレクトリ・__pycache__は除外)。"""
    if not tests_root.is_dir():
        return []
    names = [
        p.name
        for p in tests_root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and not p.name.startswith("_")
    ]
    return sorted(names)


def workflow_text(workflows_dir: Path) -> str:
    """`.github/workflows/*.yml` (`*.yaml` も含む) の全内容を連結して返す。"""
    chunks: list[str] = []
    if not workflows_dir.is_dir():
        return ""
    for path in sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml")):
        chunks.append(path.read_text())
    return "\n".join(chunks)


def find_unreferenced_dirs(test_dirs: list[str], combined_workflow_text: str) -> list[str]:
    """workflowテキストに `tests/<name>` を含まないディレクトリ名一覧を返す。"""
    return [name for name in test_dirs if f"tests/{name}" not in combined_workflow_text]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", type=Path, default=_PROJECT_ROOT)
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root
    tests_root = repo_root / "tests"
    workflows_dir = repo_root / ".github" / "workflows"

    test_dirs = discover_test_dirs(tests_root)
    if not test_dirs:
        print(f"WARNING: {tests_root} 配下にサブディレクトリが見つからなかった", file=sys.stderr)
        return 0

    combined = workflow_text(workflows_dir)
    unreferenced = find_unreferenced_dirs(test_dirs, combined)

    if unreferenced:
        print(
            "FAIL: 以下のtests/サブディレクトリが .github/workflows/*.yml のどのjobからも "
            "参照されていない(CIで実行されていない可能性):",
            file=sys.stderr,
        )
        for name in unreferenced:
            print(f"  - tests/{name}", file=sys.stderr)
        return 1

    print(f"check_test_dir_coverage: OK ({len(test_dirs)}ディレクトリ全て参照済み: {', '.join(test_dirs)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
