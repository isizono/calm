"""mypy のエラー件数を、ファイルごとの基準値（mypy-baseline.json）と比べる。

既存のエラーが残っていても CI に入れられるようにするためのラチェット。

- 基準値より件数が多いファイルがあれば失敗し、そのファイルのエラーを表示する
- 基準値より少ないファイルがあれば成功させたうえで、基準値を下げるよう案内する
- `--update` で、今の件数で基準値を書き換える

合計ではなくファイルごとに比べるのは、あるファイルで直した件数と別のファイルで
増えた件数が打ち消し合い、増えたことに気づけない状態を避けるため。
全ファイルが0件になったら、このスクリプトをやめて普通の `mypy` の実行に置き換える。

対象ファイルと mypy の設定は pyproject.toml の [tool.mypy] にある。

使い方:
    uv run python scripts/check_mypy_baseline.py
    uv run python scripts/check_mypy_baseline.py --update
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = _PROJECT_ROOT / "mypy-baseline.json"


@dataclass(frozen=True)
class MypyError:
    file: str
    line: int
    code: str | None
    message: str

    def render(self) -> str:
        code = f"  [{self.code}]" if self.code else ""
        return f"{self.file}:{self.line}: error: {self.message}{code}"


@dataclass(frozen=True)
class Comparison:
    # ファイル -> (基準値, 今の件数)
    increased: dict[str, tuple[int, int]]
    decreased: dict[str, tuple[int, int]]


class MypyRunError(RuntimeError):
    """mypy 自体が異常終了した（設定エラー・クラッシュ等）。"""


def run_mypy(repo_root: Path) -> str:
    """mypy を JSON 出力で実行し、標準出力を返す。

    終了コードは 0（エラーなし）と 1（型エラーあり）を正常とし、
    それ以外は mypy 自体の失敗として MypyRunError を送出する。
    """
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--output", "json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode not in (0, 1):
        raise MypyRunError(
            f"mypy が終了コード {result.returncode} で失敗しました:\n{result.stderr}{result.stdout}"
        )
    return result.stdout


def parse_mypy_json(output: str) -> list[MypyError]:
    """`mypy --output json` の出力（1行1件）から error だけを取り出す。

    note など error 以外の行は数えない。JSON でない行（集計行など）は読み飛ばす。
    """
    errors: list[MypyError] = []
    for raw in output.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if item.get("severity") != "error":
            continue
        errors.append(
            MypyError(
                file=str(item["file"]).replace("\\", "/"),
                line=int(item.get("line", 0)),
                code=item.get("code"),
                message=str(item.get("message", "")),
            )
        )
    return errors


def count_by_file(errors: list[MypyError]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in errors:
        counts[e.file] = counts.get(e.file, 0) + 1
    return dict(sorted(counts.items()))


def compare(baseline: dict[str, int], current: dict[str, int]) -> Comparison:
    increased: dict[str, tuple[int, int]] = {}
    decreased: dict[str, tuple[int, int]] = {}
    for file in sorted(set(baseline) | set(current)):
        base = baseline.get(file, 0)
        now = current.get(file, 0)
        if now > base:
            increased[file] = (base, now)
        elif now < base:
            decreased[file] = (base, now)
    return Comparison(increased=increased, decreased=decreased)


def load_baseline(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): int(v) for k, v in data.items()}


def write_baseline(path: Path, counts: dict[str, int]) -> None:
    nonzero = {k: v for k, v in sorted(counts.items()) if v > 0}
    path.write_text(json.dumps(nonzero, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def report(comparison: Comparison, errors: list[MypyError]) -> list[str]:
    """表示する行を組み立てる。増えたファイルはエラー本文も並べる。"""
    lines: list[str] = []
    if comparison.increased:
        lines.append("mypy のエラーが基準値（mypy-baseline.json）より増えたファイルがあります:")
        for file, (base, now) in comparison.increased.items():
            lines.append(f"  {file}: {base} -> {now}")
            lines.extend(f"    {e.render()}" for e in errors if e.file == file)
        lines.append("増えたエラーを直してください。")
    if comparison.decreased:
        lines.append("mypy のエラーが基準値より減ったファイルがあります:")
        for file, (base, now) in comparison.decreased.items():
            lines.append(f"  {file}: {base} -> {now}")
        lines.append(
            "基準値を下げてください: uv run python scripts/check_mypy_baseline.py --update"
        )
    if not lines:
        lines.append("mypy のエラー件数は基準値どおりです。")
    return lines


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--update",
        action="store_true",
        help="今のエラー件数で mypy-baseline.json を書き換える",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    repo_root: Path = _PROJECT_ROOT,
    baseline_path: Path = BASELINE_PATH,
) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        output = run_mypy(repo_root)
    except MypyRunError as e:
        print(str(e), file=sys.stderr)
        return 2

    errors = parse_mypy_json(output)
    current = count_by_file(errors)

    if args.update:
        write_baseline(baseline_path, current)
        print(f"{baseline_path.name} を更新しました（{sum(current.values())}件・{len(current)}ファイル）。")
        return 0

    comparison = compare(load_baseline(baseline_path), current)
    stream = sys.stderr if comparison.increased else sys.stdout
    for line in report(comparison, errors):
        print(line, file=stream)
    return 1 if comparison.increased else 0


if __name__ == "__main__":
    sys.exit(main())
