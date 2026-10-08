"""人の発話の印（訂正の候補）の読み書き。

UserPromptSubmit hookが、人の発話のたびにprompt_idを印として書く。記録役の見張りは
片を切るとき、印の付いた発話に「訂正候補」の札を付け、記録役に訂正かどうかを
判定させる。訂正かどうかの見分けは記録役（LLM）に任せ、ここでは人の発話かどうか
だけを見る（字面の分類では判断の層の訂正を取りこぼすため）。

印は記録役の実行ディレクトリ（recorder_runs/<sid>/）に置く。HookStateの
`{prefix}_{sid}`の置き場はSessionStartのたびに消える（compactを含む）ため使わない。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from hooks.hook_state import HookState
from hooks.turn_origin import is_nonhuman_turn

MARKS_FILE = "human_prompts.jsonl"


def marks_path(session_id: str) -> Path:
    # recorder_watch.run_dir_forと同じ規則（recorder_watchはimportが重いので複製する）
    return HookState.BASE_DIR / "recorder_runs" / session_id.replace("/", "_") / MARKS_FILE


def is_human_prompt(prompt: object) -> bool:
    """人が打った発話か。中継・通知・スラッシュコマンドの展開は除く。"""
    if not isinstance(prompt, str) or not prompt.strip():
        return False
    if is_nonhuman_turn(prompt):
        return False
    head = prompt.lstrip()[:40]
    return not head.startswith(("<command-", "<local-command", "<bash-"))


def append_mark(session_id: str, prompt_id: str, prompt: str, *, now: float | None = None) -> None:
    path = marks_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"prompt_id": prompt_id, "at": now if now is not None else time.time(), "head": prompt[:200]}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_mark_ids(session_id: str) -> set[str]:
    return read_mark_ids(marks_path(session_id))


def read_mark_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                pid = json.loads(line).get("prompt_id")
            except json.JSONDecodeError:
                continue
            if pid:
                ids.add(pid)
    except OSError:
        pass
    return ids


def prompt_text(raw: dict) -> str | None:
    """transcriptの1行が人の発話の行ならその本文を返す。そうでなければNone。"""
    if raw.get("type") != "user" or raw.get("isMeta") or raw.get("isCompactSummary"):
        return None
    content = (raw.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    return text if is_human_prompt(text) else None


def marks_from_transcript(transcript: Path) -> list[dict]:
    """hookが無かった頃のtranscriptから、hookが書いたはずの印を作り直す。"""
    marks = []
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict) or not raw.get("promptId"):
            continue
        text = prompt_text(raw)
        if text is not None:
            marks.append({"prompt_id": raw["promptId"], "timestamp": raw.get("timestamp"), "head": text[:200]})
    return marks


_PROMPT_ID_RE = re.compile(r"prompt_id:\s*`?([0-9A-Za-z-]+)")


def new_unresolved(state: HookState, kind: str, session_id: str) -> list[dict]:
    """このセッションが受けた訂正の未解消の未教訓化のうち、kindでまだ扱っていないものを返す。

    受けたかどうかは、未教訓化の本文のprompt_idがこのセッションの印にあるかで見る。
    印に無い（訂正を受けていない個体・印を書く前の発話）なら返さない。対象はcheck-in中の
    activityに直接つながるか、その関連topicに属する件。DBを読めないときは空（知らせない・
    止めない側）に倒す。
    """
    activity_id = state.get_checked_in_activity()
    received = load_mark_ids(session_id)
    if activity_id is None or not received:
        return []
    from src.db import get_connection
    from src.services.correction_service import unresolved_corrections

    conn = get_connection(load_vec=False)
    try:
        topic_ids = [
            r[0] for r in conn.execute(
                "SELECT target_id FROM relations_view WHERE source_type = 'activity' AND source_id = ?"
                " AND target_type = 'topic'",
                (activity_id,),
            )
        ]
        items, _ = unresolved_corrections(conn, activity_id, topic_ids, 50)
        contents = {
            i["id"]: conn.execute("SELECT content FROM materials WHERE id = ?", (i["id"],)).fetchone()[0]
            for i in items
        }
    finally:
        conn.close()
    seen = state.get_correction_ids(kind)
    return [
        i for i in items
        if i["id"] not in seen and (m := _PROMPT_ID_RE.search(contents.get(i["id"]) or "")) and m.group(1) in received
    ]
