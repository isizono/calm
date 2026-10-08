#!/usr/bin/env python3
"""新しい矛盾・摩擦のsignalが起票されるまで待ち、起票されたら終わる。

対話セッションがBashのrun_in_backgroundで呼ぶ待ち受け。終了すると
Claude Codeがそのセッションを起こすので、stdoutに見つけたsignalと
仕掛け直しのコマンドを出す。

- --after より大きいidの行だけを見る。仕掛け直すまでの間に来た行を
  落とさないよう、仕掛け直しのコマンドには今回見た最大のidを渡す
- 同じfingerprintの再来はoccurrence_countが増えるだけで新しい行に
  ならないので拾わない
- 待ち時間の上限に達したら、黙って死なずに仕掛け直しを促して終わる
- CLAUDE_PIDのプロセスが居なくなったら何も出さずに終わる

DBは毎周期immutableで開き直して読むだけ。Bashのsandbox内では
mode=roだけでは開けない（2026-10-09実測）。immutableは開いている間
変更を見ないので、接続は周期ごとに閉じる。読み取りの失敗は次の周期に回す。
標準ライブラリのみに依存する。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".claude" / ".claude-code-memory" / "discussion.db"
KINDS = ("contradiction", "friction")
POLL_SECONDS = 15
# ask回答のrewakeと同じく24時間より手前で自分から終わる
WAIT_LIMIT_SECONDS = 86400 - 300


def fetch_new(db_path: str, after: int) -> list[tuple] | None:
    """after より大きいidの対象kindの行を返す。読めなければNone。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return None
    try:
        return conn.execute(
            "SELECT id, kind, summary FROM signal_events"
            " WHERE id > ? AND kind IN (?, ?) ORDER BY id",
            (after, *KINDS),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def current_max_id(db_path: str) -> int | None:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        try:
            return conn.execute("SELECT COALESCE(MAX(id), 0) FROM signal_events").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def rearm_command(after: int) -> str:
    return f"python3 -I {Path(__file__).resolve()} --after {after}"


def wake_message(rows: list[tuple]) -> str:
    ids = [r[0] for r in rows]
    lines = ["CALM: 新しい矛盾・摩擦のsignalが起票された。"]
    lines += [f"- #{i} {kind}: {summary[:120]}" for i, kind, summary in rows]
    lines.append(f"get_signals(ids={ids}, status=null) で全文を読み、トリアージする。")
    lines.append(f"終わったら run_in_background で仕掛け直す: {rearm_command(max(ids))}")
    return "\n".join(lines)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv=None, *, sleep=time.sleep, now=time.time) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--after", type=int, help="このidより後の行を待つ。省略時は今の最大id")
    p.add_argument("--db", default=os.environ.get("CALM_DB_PATH", str(DEFAULT_DB_PATH)))
    args = p.parse_args(argv)

    after = args.after
    while after is None:
        after = current_max_id(args.db)
        if after is None:
            sleep(POLL_SECONDS)
    pid = int(os.environ["CLAUDE_PID"]) if os.environ.get("CLAUDE_PID", "").isdigit() else None
    start = now()

    while True:
        if pid is not None and not _alive(pid):
            return 0
        rows = fetch_new(args.db, after)
        if rows:
            print(wake_message(rows), flush=True)
            return 0
        if now() - start > WAIT_LIMIT_SECONDS:
            print(f"CALM: signalの待ち受けが時間切れで終わった。仕掛け直す: {rearm_command(after)}",
                  flush=True)
            return 0
        sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
