#!/usr/bin/env python3
"""tests/unit/ 配置lint: 実プロセスをsubprocessで起動しているファイルを検出する。

`tests/unit/` は in-process で完結するテストの置き場であり、実プロセス起動を
伴うテストは `tests/e2e/` に置く規約になっている(docs/spec/test-convention.md §4)。
この規約から外れた新規ファイルの紛れ込みを機械的に検知する。

検出パターンは `subprocess.run(` / `subprocess.Popen(` / `Popen(` /
`subprocess.call(` / `subprocess.check_call(` / `subprocess.check_output(` /
`os.system(` / `os.popen(` の呼び出し構文(開き括弧まで)。docstringやコメント
中の「subprocess.Popenが呼ばれる」のような散文的な言及は開き括弧を伴わない
ため誤検知しない。

`scripts/test_pyramid_allowlist.txt` に列挙済みの既存ファイルは除外し、
リストに無い新規ファイルでパターンが見つかった場合のみエラー終了する。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
_DEFAULT_ALLOWLIST = _SCRIPT_DIR / "test_pyramid_allowlist.txt"

_SUBPROCESS_CALL_RE = re.compile(
    r"subprocess\.(run|Popen|call|check_call|check_output)\(|\bPopen\(|os\.system\(|os\.popen\("
)


def load_allowlist(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    names: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(line)
    return names


def find_violations(unit_dir: Path, allowlist: set[str]) -> list[str]:
    """subprocess呼び出しパターンを含み、かつ許可リストに無いファイル名一覧を返す(ソート済み)。"""
    if not unit_dir.is_dir():
        return []
    violations: list[str] = []
    for path in sorted(unit_dir.glob("*.py")):
        if path.name in allowlist:
            continue
        text = path.read_text()
        if _SUBPROCESS_CALL_RE.search(text):
            violations.append(path.name)
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", type=Path, default=_PROJECT_ROOT)
    parser.add_argument("--allowlist", type=Path, default=_DEFAULT_ALLOWLIST)
    args = parser.parse_args(argv)

    unit_dir = args.repo_root / "tests" / "unit"
    allowlist = load_allowlist(args.allowlist)

    violations = find_violations(unit_dir, allowlist)

    if violations:
        print(
            "FAIL: 以下のtests/unit/配下のファイルが実プロセスのsubprocess起動を含んでいる"
            "(規約上はtests/e2e/相当。意図的なら scripts/test_pyramid_allowlist.txt に追加すること):",
            file=sys.stderr,
        )
        for name in violations:
            print(f"  - tests/unit/{name}", file=sys.stderr)
        return 1

    print(f"check_unit_subprocess_placement: OK (許可リスト{len(allowlist)}件を除外して違反なし)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
