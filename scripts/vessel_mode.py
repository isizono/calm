#!/usr/bin/env python3
"""器の停止スイッチ（vessel_meta.mode）の読み取り・切り替えCLI。

使い方:
    uv run python scripts/vessel_mode.py get
    uv run python scripts/vessel_mode.py set <off|observe|on>

hookは毎回 mode を読むため、切り替えは動いている全セッションの次のhook
呼び出しから効く。off にすると観測・配達・差し戻しのすべてが止まる。
observe（既定）は観測だけを行い、配達・差し戻し・運用の1行は出さない。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.db import get_connection  # noqa: E402

_VALID_MODES = ("off", "observe", "on")


def cmd_get(_args: argparse.Namespace) -> int:
    conn = get_connection()
    try:
        row = conn.execute("SELECT mode FROM vessel_meta WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        print("vessel_meta に行が無い（migration未適用の可能性がある）", file=sys.stderr)
        return 1
    print(row["mode"])
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE vessel_meta SET mode = ? WHERE id = 1", (args.mode,))
        conn.commit()
        if cur.rowcount == 0:
            print("vessel_meta に行が無い（migration未適用の可能性がある）", file=sys.stderr)
            return 1
    finally:
        conn.close()
    print(f"mode を {args.mode} にした")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("get", help="現在のmodeを表示する")
    p_set = sub.add_parser("set", help="modeを切り替える")
    p_set.add_argument("mode", choices=_VALID_MODES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "get":
        return cmd_get(args)
    return cmd_set(args)


if __name__ == "__main__":
    sys.exit(main())
