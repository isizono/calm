#!/usr/bin/env python3
"""orchの担い手(または相談役)のセッションが生きているかを判定する。

`claude agents --json`の行と、transcriptの最終更新時刻・末尾の行から、OK / DEAD / STUCK を
1行のJSONでstdoutへ出す。

- DEAD: sessionIdが一致しpidがある行が無い
- STUCK: その行(複数あればstartedAtが最も新しい行)のstatusがbusyのまま、
  transcriptが--stuck-minutes分以上更新されておらず、かつtranscriptの末尾が
  ターンの終わりの行(system/turn_duration)でない
- OK: それ以外(transcriptが見つからないときは固まりを判定できないのでOK)

ターンを終えたあとも、裏のshell(Bashのバックグラウンド等)がstatusをbusyに保つことがある。
その待機中を固まりと読まないよう、transcriptの末尾の種類をtranscript_tailとして出力に添える。

標準ライブラリのみに依存する。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

TURN_END = "system/turn_duration"


def judge(rows: list[dict], session_id: str, transcript_mtime: float | None,
          now: float, stuck_minutes: float, tail: str | None = None) -> dict:
    live = [r for r in rows if r.get("sessionId") == session_id and r.get("pid")]
    if not live:
        return {"verdict": "DEAD", "session_id": session_id}
    row = max(live, key=lambda r: r.get("startedAt", 0))
    result = {"verdict": "OK", "session_id": session_id, "pid": row["pid"],
              "status": row.get("status")}
    if transcript_mtime is not None:
        age_min = (now - transcript_mtime) / 60
        result["transcript_age_min"] = round(age_min, 1)
        if row.get("status") == "busy" and age_min >= stuck_minutes and tail != TURN_END:
            result["verdict"] = "STUCK"
    return result


def _line_kind(raw: bytes) -> str | None:
    """会話の進みを示す行なら種類("assistant/tool_use"等)、それ以外はNone。

    attachment・queue-operation・timestampの無い帳簿行(cost-state等)と、turn_duration以外の
    system行(stop_hook_summaryはhookがblockすると後に続くので終わりではない。informationalは
    待機中にも届く)は会話の進みではないので飛ばす。
    """
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    kind = d.get("type")
    if kind == "system":
        return TURN_END if d.get("subtype") == "turn_duration" else None
    if kind not in ("user", "assistant"):
        return None
    content = (d.get("message") or {}).get("content")
    first = content[0].get("type", "text") if isinstance(content, list) and content else "text"
    return f"{kind}/{first}"


def transcript_tail(path: Path) -> str | None:
    """transcript末尾の会話行の種類。末尾から読み、見つかるまで読む範囲を広げる。"""
    size = path.stat().st_size
    chunk = 1 << 16
    with path.open("rb") as f:
        while True:
            f.seek(max(0, size - chunk))
            lines = f.read().split(b"\n")
            if chunk < size:
                lines = lines[1:]  # 先頭は途中から読んだ行
            for raw in reversed(lines):
                kind = _line_kind(raw)
                if kind:
                    return kind
            if chunk >= size:
                return None
            chunk *= 4


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
    tail = transcript_tail(transcript) if transcript else None
    result = judge(json.loads(out), args.session_id,
                   transcript.stat().st_mtime if transcript else None,
                   time.time(), args.stuck_minutes, tail=tail)
    if transcript:
        result["transcript"] = str(transcript)
        result["transcript_tail"] = tail
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
