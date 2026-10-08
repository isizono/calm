#!/usr/bin/env python3
"""orchの担い手(または相談役)のセッションが生きているかを判定する。

`claude agents --json`の行とtranscriptの最終更新時刻から、OK / DEAD / STUCK を
1行のJSONでstdoutへ出す。

- DEAD: sessionIdが一致しpidがある行が無い
- STUCK: その行(複数あればstartedAtが最も新しい行)のstatusがbusyのまま、
  transcriptが--stuck-minutes分以上更新されていない
- OK: それ以外(transcriptが見つからないときは固まりを判定できないのでOK)

標準ライブラリのみに依存する。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


def judge(rows: list[dict], session_id: str, transcript_mtime: float | None,
          now: float, stuck_minutes: float) -> dict:
    live = [r for r in rows if r.get("sessionId") == session_id and r.get("pid")]
    if not live:
        return {"verdict": "DEAD", "session_id": session_id}
    row = max(live, key=lambda r: r.get("startedAt", 0))
    result = {"verdict": "OK", "session_id": session_id, "pid": row["pid"],
              "status": row.get("status")}
    if transcript_mtime is not None:
        age_min = (now - transcript_mtime) / 60
        result["transcript_age_min"] = round(age_min, 1)
        if row.get("status") == "busy" and age_min >= stuck_minutes:
            result["verdict"] = "STUCK"
    return result


def find_transcript(session_id: str, projects_dir: Path) -> Path | None:
    hits = list(projects_dir.glob(f"*/{session_id}.jsonl"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--session-id", required=True, help="担い手欄(または相談役)のsessionId")
    parser.add_argument("--stuck-minutes", type=float, default=25,
                        help="busyのままtranscriptが更新されないと固まりとみなす分数(既定: 25)")
    parser.add_argument("--projects-dir", default=str(Path.home() / ".claude" / "projects"),
                        help="transcriptを探すディレクトリ(既定: ~/.claude/projects)")
    args = parser.parse_args(argv)
    out = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True,
                         check=True).stdout
    transcript = find_transcript(args.session_id, Path(args.projects_dir))
    result = judge(json.loads(out), args.session_id,
                   transcript.stat().st_mtime if transcript else None,
                   time.time(), args.stuck_minutes)
    if transcript:
        result["transcript"] = str(transcript)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
