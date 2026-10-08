#!/usr/bin/env python3
"""人の訂正を未教訓化として持ち越す仕組みの手段。

サブコマンド:
- marks: transcriptから人の発話の印を作り直す（hookの導入前のtranscriptを記録役に通すとき）
- context: 訂正の前後をtranscriptから切り出す。経緯を持たない個体（Agentのthinker）に
  切り出しと訂正だけを渡して、同じ型の他の誤りを洗わせるための入力
- observe: 教訓が届いたかを、書き手でないこのスクリプトが確かめ、届いていれば
  観測の記録（素タグlesson-observed）を未教訓化に結ぶ
    - checkin: DBの複製に対し、新しいセッションとしてcheck_inした応答に、教訓の文が全文で出るか
      （複製に対して行うので、本物のactivityの状態・既出記録は変えない）
    - feedback-entry: 届け先の記録より後に、そのフィードバックエントリが配達されたセッションがあるか
- metrics: 訂正から24時間以内に届け先が書かれた率と、同じ型の再来の数

基準の19.5%（資材「feedback未記入の再計測: 最終集計」）は、人の訂正190件について
24時間以内の書き込みが教訓を記録しているかを照合係が判定した値である。metricsの率は
記録役が訂正と判定した件のうち、24時間以内にlesson-deliveryの記録が結ばれた割合で、
分母の判定者と分子の数え方が違う。比べるときはこの差を添える。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.correction_marks import (  # noqa: E402
    MARKS_FILE,
    marks_from_transcript,
    prompt_text,
)

BASELINE_RATE = 0.195
_UTTERED_AT_RE = re.compile(r"発話時刻:\s*`?([0-9T:\-\.Z ]+)")


def cmd_marks(args: argparse.Namespace) -> None:
    marks = marks_from_transcript(Path(args.transcript))
    if args.into_run_dir:
        path = Path(args.into_run_dir) / MARKS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for m in marks:
                f.write(json.dumps({"prompt_id": m["prompt_id"], "head": m["head"]}, ensure_ascii=False) + "\n")
    for m in marks:
        print(json.dumps(m, ensure_ascii=False))


def cmd_context(args: argparse.Namespace) -> None:
    from hooks.recorder_watch import _is_chunkable, _normalize_line_text, _read_lines

    lines = [line for line in _read_lines(Path(args.transcript), 0)[0] if _is_chunkable(line.entry)]
    idx = next(
        (i for i, line in enumerate(lines)
         if line.raw.get("promptId") == args.prompt_id and prompt_text(line.raw) is not None),
        None,
    )
    if idx is None:
        sys.exit(f"prompt_id {args.prompt_id} がtranscriptに無い")
    for line in lines[max(0, idx - args.before): idx + args.after + 1]:
        text = _normalize_line_text(line.entry)
        if text:
            mark = ">>> 訂正 >>> " if line is lines[idx] else ""
            print(mark + text + "\n")


def _correction(conn: sqlite3.Connection, material_id: int) -> dict:
    row = conn.execute("SELECT id, title, content FROM materials WHERE id = ?", (material_id,)).fetchone()
    if row is None:
        sys.exit(f"material {material_id} が無い")
    domains = [
        r[0] for r in conn.execute(
            "SELECT 'domain:' || t.name FROM material_tags mt JOIN tags t ON t.id = mt.tag_id"
            " WHERE mt.material_id = ? AND t.namespace = 'domain'",
            (material_id,),
        )
    ]
    return {"id": row[0], "title": row[1], "content": row[2], "domains": domains or ["domain:calm"]}


def _observe_checkin(activity_id: int, needle: str) -> tuple[bool, str]:
    from src.db import get_db_path

    src_path = get_db_path()
    with tempfile.TemporaryDirectory() as tmp:
        copy_path = os.path.join(tmp, "observe.db")
        with sqlite3.connect(src_path) as src, sqlite3.connect(copy_path) as dst:
            src.backup(dst)
        prev = os.environ.get("CALM_DB_PATH")
        os.environ["CALM_DB_PATH"] = copy_path
        try:
            from src.main import _check_in, _finalize_checkin_result

            result = _finalize_checkin_result(_check_in(activity_id, session_id=f"observe-{uuid.uuid4()}"), "internal")
        finally:
            if prev is None:
                os.environ.pop("CALM_DB_PATH", None)
            else:
                os.environ["CALM_DB_PATH"] = prev
    dump = json.dumps(result, ensure_ascii=False)
    found = needle in dump
    return found, f"経緯の無い新しいセッションとしてactivity {activity_id}にcheck_inした応答（{len(dump)}字）に、文「{needle}」が{'全文で出た' if found else '出なかった'}"


def _observe_feedback_entry(conn: sqlite3.Connection, correction_id: int, entry_id: int) -> tuple[bool, str]:
    from src.services.correction_service import correction_stats

    stat = next((s for s in correction_stats(conn) if s["id"] == correction_id), None)
    since = stat and stat["delivered_at"]
    if not since:
        return False, "届け先の記録（lesson-delivery）がまだ無いので、配達の観測を始められない"
    rows = conn.execute(
        "SELECT DISTINCT session_id FROM feedback_turn_marks WHERE entry_id = ? AND created_at > ?",
        (entry_id, since),
    ).fetchall()
    sessions = [r[0] for r in rows]
    found = bool(sessions)
    return found, (
        f"届け先の記録（{since}）より後に、フィードバックエントリ{entry_id}が配達されたセッション: "
        f"{len(sessions)}件 {sessions[:5]}"
    )


def cmd_observe(args: argparse.Namespace) -> None:
    from src.db import get_connection

    conn = get_connection(load_vec=False)
    try:
        corr = _correction(conn, args.correction)
        if args.mode == "checkin":
            if not (args.activity and args.needle):
                sys.exit("checkinには--activityと--needleが要る")
            found, evidence = _observe_checkin(args.activity, args.needle)
        else:
            if not args.entry:
                sys.exit("feedback-entryには--entryが要る")
            found, evidence = _observe_feedback_entry(conn, corr["id"], args.entry)
    finally:
        conn.close()
    print(json.dumps({"correction": corr["id"], "found": found, "evidence": evidence}, ensure_ascii=False))
    if found and args.record:
        from src.services.correction_service import LESSON_OBSERVED_TAG
        from src.services.material_service import add_material

        res = add_material(
            title=f"観測: {corr['title']}"[:35],
            content=(
                f"未教訓化「{corr['title']}」の教訓が届いたことの観測。書き手ではない scripts/corrections.py observe "
                f"（{args.mode}）が確かめた。\n\n- 観測時刻: {datetime.now().isoformat(timespec='seconds')}\n- 結果: {evidence}"
            ),
            tags=[*corr["domains"], LESSON_OBSERVED_TAG],
            source="scripts/corrections.py observe",
            related=[{"type": "material", "ids": [corr["id"]]}],
        )
        print(json.dumps(res, ensure_ascii=False, default=str))


def _utc(raw: str) -> datetime:
    """DBの時刻（UTCの"YYYY-MM-DD HH:MM:SS"）とtranscriptの時刻（ISO、Z付き）を、tz無しのUTCに揃える。"""
    dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def _uttered_at(content: str, created_at: str) -> datetime:
    """未教訓化の本文の「発話時刻」を読む。読めなければ積まれた時刻で代える。"""
    m = _UTTERED_AT_RE.search(content or "")
    try:
        return _utc(m.group(1)) if m else _utc(created_at)
    except ValueError:
        return _utc(created_at)


def cmd_metrics(args: argparse.Namespace) -> None:
    from src.db import get_connection
    from src.services.correction_service import correction_stats

    conn = get_connection(load_vec=False)
    try:
        stats = correction_stats(conn)
        contents = {s["id"]: conn.execute("SELECT content FROM materials WHERE id = ?", (s["id"],)).fetchone()[0]
                    for s in stats}
    finally:
        conn.close()
    n = len(stats)
    within = 0
    for s in stats:
        if not s["delivered_at"]:
            continue
        uttered = _uttered_at(contents.get(s["id"], ""), s["created_at"])
        if _utc(s["delivered_at"]) - uttered <= timedelta(hours=24):
            within += 1
    out = {
        "corrections": n,
        "delivered_within_24h": within,
        "rate_within_24h": round(within / n, 3) if n else None,
        "baseline_rate": BASELINE_RATE,
        "resolved_observed": sum(1 for s in stats if s["delivered_at"] and s["observed_at"]),
        "delivered_unobservable": sum(
            1 for s in stats if s["delivered_at"] and s["unobservable"] and not s["observed_at"]),
        "unresolved": sum(
            1 for s in stats if not (s["delivered_at"] and (s["observed_at"] or s["unobservable"]))),
        "same_type_recurrences": sum(1 for s in stats if s["same_type_of"]),
        "note": "率は記録役が訂正と判定した件のうち24時間以内にlesson-deliveryが結ばれた割合。基準は照合係判定の別定義",
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("marks", help="transcriptから人の発話の印を作り直す")
    p.add_argument("--transcript", required=True)
    p.add_argument("--into-run-dir", help="記録役の実行ディレクトリに印を追記する")
    p.set_defaults(func=cmd_marks)

    p = sub.add_parser("context", help="訂正の前後をtranscriptから切り出す")
    p.add_argument("--transcript", required=True)
    p.add_argument("--prompt-id", required=True)
    p.add_argument("--before", type=int, default=30)
    p.add_argument("--after", type=int, default=6)
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("observe", help="教訓が届いたかを確かめ、届いていれば観測を記録する")
    p.add_argument("mode", choices=["checkin", "feedback-entry"])
    p.add_argument("--correction", type=int, required=True, help="未教訓化のmaterial id")
    p.add_argument("--activity", type=int)
    p.add_argument("--needle", help="check_inの応答に全文で出るべき教訓の文")
    p.add_argument("--entry", type=int, help="フィードバックエントリのid")
    p.add_argument("--record", action="store_true", help="届いていれば観測の記録を書く")
    p.set_defaults(func=cmd_observe)

    p = sub.add_parser("metrics", help="24時間以内の教訓化率と同じ型の再来の数")
    p.set_defaults(func=cmd_metrics)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
