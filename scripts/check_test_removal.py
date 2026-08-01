#!/usr/bin/env python3
"""テスト消失検知: base commitからhead commitにかけて、気づかれずに消えた
テスト関数が無いかを検証する。

base/head それぞれのcommitを一時worktreeにcheckoutし、`pytest --collect-only`
で収集したnode ID集合を関数レベル(parametrize展開前の
`モジュールパス::クラス名::関数名`。`[...]` のparametrize suffixは比較対象から
除く)に正規化して比較する。base側のnode IDはgitのrename検出結果でファイル
パス部分を付け替えてから比較するため、単純なファイルrename(関数自体は無傷)
は「消えた」扱いにならない。baseに存在しheadで消えた関数があれば、PR本文に
`[test-removal: ...]` で始まるマーカー行が含まれているかを確認し、無ければ
消えた関数一覧を表示してエラー終了する。

使い方:
    uv run python scripts/check_test_removal.py --base <ref> --head <ref>

PR本文をチェック対象に含めるには環境変数 CCM_PR_BODY にPR本文を渡す
(GitHub Actions では `${{ github.event.pull_request.body }}` を渡す想定。
`scripts/lint_doc_cochange.py` の前例に倣う)。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent

TEST_REMOVAL_MARKER_PREFIX = "[test-removal: "

# pytestのnode ID行の先頭判定に使う。node IDはファイルパスから始まり、
# parametrize idに空白や記号が混じっても先頭の `<path>.py::` 部分に空白は
# 出現しないため、行頭一致で十分に判別できる。
_NODEID_LINE_RE = re.compile(r"^\S+\.py::")


def to_function_id(node_id: str) -> str:
    """pytest node ID からparametrize suffix(`[...]`)を取り除いた関数レベルIDを返す。

    `path::Class::method[params]` -> `path::Class::method`
    `path::function[params]` -> `path::function`
    """
    parts = node_id.split("::")
    last = parts[-1]
    if "[" in last:
        last = last.split("[", 1)[0]
    parts[-1] = last
    return "::".join(parts)


def parse_collect_output(stdout: str) -> set[str]:
    """`pytest --collect-only -q` の標準出力から関数レベルのnode ID集合を抽出する。"""
    ids: set[str] = set()
    for line in stdout.splitlines():
        line = line.rstrip("\n")
        if _NODEID_LINE_RE.match(line):
            ids.add(to_function_id(line))
    return ids


def collect_test_ids(repo_root: Path, ref: str, *, uv_sync: bool = True) -> set[str]:
    """指定refを一時worktreeにcheckoutし、pytest --collect-onlyで関数レベルの
    node ID集合を取得する。

    実行環境(依存パッケージ)もref時点のpyproject.toml/uv.lockに合わせて
    `uv sync --frozen` で再構築するため、ref間でテスト対象コードやテスト自体の
    import 可否が変わっていても正しく収集できる。
    """
    with tempfile.TemporaryDirectory(prefix="test-pyramid-collect-") as tmp:
        worktree_dir = Path(tmp) / "wt"
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", "-f", str(worktree_dir), ref],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if add.returncode != 0:
            raise RuntimeError(
                f"'git worktree add' に失敗した(ref={ref}):\n{add.stdout}\n{add.stderr}"
            )
        try:
            if uv_sync:
                sync = subprocess.run(
                    ["uv", "sync", "--frozen"],
                    cwd=worktree_dir,
                    capture_output=True,
                    text=True,
                )
                if sync.returncode != 0:
                    raise RuntimeError(
                        f"'uv sync --frozen' に失敗した(ref={ref}):\n{sync.stdout}\n{sync.stderr}"
                    )

            tests_dir = worktree_dir / "tests"
            if not tests_dir.is_dir():
                return set()

            collect = subprocess.run(
                ["uv", "run", "pytest", "--collect-only", "-q", "tests"],
                cwd=worktree_dir,
                capture_output=True,
                text=True,
            )
            # pytestの終了コード: 0=正常, 2=一部収集エラーを含む(部分結果は使える),
            # 5=収集0件。それ以外は環境自体が壊れている可能性が高く、
            # 「テストが消えた」との誤判定を避けるためハードエラーにする。
            if collect.returncode not in (0, 2, 5):
                raise RuntimeError(
                    f"'pytest --collect-only' が異常終了した(ref={ref}, "
                    f"returncode={collect.returncode}):\n{collect.stdout}\n{collect.stderr}"
                )
            if collect.returncode == 2:
                print(
                    f"WARNING: ref={ref} のテスト収集で一部エラーが発生した"
                    "(収集できたテストのみで比較する):",
                    file=sys.stderr,
                )
                print(collect.stderr, file=sys.stderr)

            return parse_collect_output(collect.stdout)
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_dir)],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )


def has_removal_marker(pr_body: str) -> bool:
    return any(line.strip().startswith(TEST_REMOVAL_MARKER_PREFIX) for line in pr_body.splitlines())


def detect_file_renames(repo_root: Path, base: str, head: str) -> dict[str, str]:
    """base..headのマージベース基準diffから、tests/配下のファイルrenameを
    `{旧パス: 新パス}` の辞書として返す。

    ファイルrenameだけでnode IDのファイルパス部分が変わり、関数自体は
    無傷でも「消えた」と誤検知されるのを防ぐための正規化に使う。
    """
    result = subprocess.run(
        ["git", "diff", "--name-status", "-M", f"{base}...{head}", "--", "tests/"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}

    renames: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[0].startswith("R"):
            _status, old_path, new_path = fields
            renames[old_path] = new_path
    return renames


def apply_renames(ids: set[str], renames: dict[str, str]) -> set[str]:
    """node ID集合のファイルパス部分をrenameマップで新パスへ付け替える。"""
    if not renames:
        return ids
    remapped: set[str] = set()
    for node_id in ids:
        path, sep, rest = node_id.partition("::")
        new_path = renames.get(path, path)
        remapped.add(f"{new_path}{sep}{rest}" if sep else new_path)
    return remapped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="比較元ref(例: PRのbase commit sha)")
    parser.add_argument("--head", required=True, help="比較先ref(例: PRのhead commit sha)")
    parser.add_argument("--repo-root", type=Path, default=_PROJECT_ROOT)
    parser.add_argument(
        "--no-uv-sync",
        action="store_true",
        help="worktree内でuv syncを実行しない(呼び出し元が既に依存関係を揃えている場合の高速化用)",
    )
    args = parser.parse_args(argv)

    pr_body = os.environ.get("CCM_PR_BODY", "")

    base_ids = collect_test_ids(args.repo_root, args.base, uv_sync=not args.no_uv_sync)
    head_ids = collect_test_ids(args.repo_root, args.head, uv_sync=not args.no_uv_sync)

    renames = detect_file_renames(args.repo_root, args.base, args.head)
    base_ids = apply_renames(base_ids, renames)

    removed = sorted(base_ids - head_ids)

    if not removed:
        print("check_test_removal: OK (消えたテスト関数なし)")
        return 0

    if has_removal_marker(pr_body):
        print(
            f"check_test_removal: {len(removed)}件のテスト関数が削除されているが、"
            f"PR本文の '{TEST_REMOVAL_MARKER_PREFIX}...' マーカーを確認したため許可する:"
        )
        for name in removed:
            print(f"  - {name}")
        return 0

    print(
        f"FAIL: base({args.base})に存在していた以下の{len(removed)}件のテスト関数が"
        f"head({args.head})で検出されなくなった:",
        file=sys.stderr,
    )
    for name in removed:
        print(f"  - {name}", file=sys.stderr)
    print(
        f"意図的な削除であれば、PR本文に '{TEST_REMOVAL_MARKER_PREFIX}<理由>]' 形式の"
        "マーカー行を含めること。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
