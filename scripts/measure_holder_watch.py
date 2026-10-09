#!/usr/bin/env python3
"""担い手欄の見張りが、担い手の停止から知らせ（signalと人宛てask）を立てるまでの時間を測る。

本番のDB・sessions・transcriptには触らない。一時ディレクトリにDBとCLIセッション
ファイルとtranscriptを作り、ダミーの担い手（sleepのプロセス）を担い手欄に書いた
使い捨てのorchを置く。見張りのスレッドを短い間隔で回し、ダミーをkillしてから
signalとaskの行ができるまでを測る。本番での検知時間はおよそ
「停止とみなす分数（CALM_HOLDER_WATCH_DEAD_MIN）＋最大で見張りの間隔」になる。

使い方: uv run python scripts/measure_holder_watch.py [--interval 5] [--dead 10]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--interval", type=float, default=5, help="見張りの間隔（秒）")
    parser.add_argument("--dead", type=float, default=10, help="停止とみなすまでの秒数")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="holder-watch-"))
    os.environ["CALM_DB_PATH"] = str(tmp / "calm.db")
    os.environ["CALM_CLAUDE_SESSIONS_DIR"] = str(tmp / "sessions")
    os.environ["CALM_CLAUDE_PROJECTS_DIR"] = str(tmp / "projects")
    os.environ["CALM_SESSION_REGISTRY_PATH"] = str(tmp / "aliases.json")

    import src.services.embedding_service as emb
    from src.db import get_connection, init_database
    from src.services.activity_service import add_activity
    from src.services.holder_watch_service import HolderWatch

    # 本番のembeddingサーバーへ接続・起動しない
    emb._backfill_done = True
    emb._is_server_running = lambda: False
    emb._start_server = lambda: None

    init_database()
    sid = str(uuid.uuid4())
    dummy = subprocess.Popen(["sleep", "600"])
    (tmp / "sessions").mkdir()
    (tmp / "sessions" / f"{dummy.pid}.json").write_text(json.dumps(
        {"pid": dummy.pid, "sessionId": sid, "name": "dummy-holder", "status": "idle",
         "startedAt": int(time.time() * 1000)}))
    transcript = tmp / "projects" / "-dummy" / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    aid = add_activity(
        title="[統合] 見張りの計測用ダミー",
        description=f"## 状態\n担い手: dummy-holder（sessionId {sid}）／計測\n",
        tags=["domain:calm", "intent:discuss", "orch"], check_in=False,
    )["activity_id"]

    HolderWatch(interval_sec=args.interval, dead_sec=args.dead, stale_sec=10**9).start()
    time.sleep(args.interval * 1.5)  # 生きている間に何も立たないことを1周以上見る
    conn = get_connection()
    early = conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0]

    dummy.kill()
    dummy.wait()
    os.utime(transcript)  # 最後のtranscript書き込みを停止の瞬間とする
    killed_at = time.time()

    signal_at = ask_at = None
    deadline = killed_at + args.dead + args.interval * 3
    while time.time() < deadline and not (signal_at and ask_at):
        if ask_at is None and conn.execute(
            "SELECT 1 FROM ask_blocks WHERE activity_id = ?", (aid,)).fetchone():
            ask_at = time.time()
        if signal_at is None and conn.execute(
            "SELECT 1 FROM signal_events WHERE kind = 'custom:holder-down'").fetchone():
            signal_at = time.time()
        time.sleep(0.1)

    question = conn.execute(
        "SELECT a.question FROM asks a JOIN ask_blocks b ON b.ask_id = a.id "
        "WHERE b.activity_id = ?", (aid,)).fetchone()
    print(json.dumps({
        "interval_sec": args.interval,
        "dead_sec": args.dead,
        "signals_while_alive": early,
        "ask_after_kill_sec": None if ask_at is None else round(ask_at - killed_at, 1),
        "signal_after_kill_sec": None if signal_at is None else round(signal_at - killed_at, 1),
        "ask_question": question[0] if question else None,
        "tmp_dir": str(tmp),
    }, ensure_ascii=False, indent=1))
    conn.close()


if __name__ == "__main__":
    main()
