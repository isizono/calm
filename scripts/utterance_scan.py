#!/usr/bin/env python3
"""過去の人間の発話を走査し、フィードバックエントリの照合条件を当て直す。

サブコマンド:
  match REGEX     人間の発話のうち、utteranceエントリの照合（hooks/feedback_hook.py の
                  _matches）で REGEX に当たるものを一覧にする
  delivered NAME  エントリ NAME の本文が UserPromptSubmit の配達に載ったターンについて、
                  配達の親にあたる人間の発話を一覧にする

発話の出どころ（--source）:
  transcripts  ~/.claude/projects 配下のtranscript（サブエージェント分を除く）。割り込み
               （queued_command）の発話も拾う。保持期間で古いものは残っていない
  history      ~/.claude/history.jsonl の入力欄の記録。期間は長いが、貼り付けは
               [Pasted text #N] に畳まれ、スラッシュコマンドも混ざる
人間の発話かどうかは hooks/turn_origin.py の判定をそのまま使う。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.feedback_hook import _connect, _matches  # noqa: E402
from hooks.turn_origin import is_nonhuman_turn  # noqa: E402

CLAUDE_HOME = Path.home() / ".claude"


def _text_of(content: object) -> str | None:
    """userメッセージのcontentから発話文を取り出す。tool_result等を含むものはNone。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        if any(not isinstance(b, dict) or b.get("type") not in ("text", "image") for b in content):
            return None
        return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
    return None


def _utterance_of(d: dict) -> str | None:
    """transcriptの1行が人間の発話ならその文を返す。"""
    if d.get("type") == "user" and not d.get("isMeta") and not d.get("toolUseResult"):
        text = _text_of(d.get("message", {}).get("content"))
    elif d.get("type") == "attachment" and d.get("attachment", {}).get("type") == "queued_command":
        text = d["attachment"].get("prompt")
        text = text if isinstance(text, str) else None
    else:
        return None
    if not text or is_nonhuman_turn(text):
        return None
    return text


def _transcript_files(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*/*.jsonl"))


def _read_lines(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def iter_transcript_utterances(root: Path) -> Iterator[tuple[str, str, str]]:
    """(timestamp, 出どころ, 発話) を返す。resumeで複製された発話は本文で重複を除く。"""
    seen: set[str] = set()
    for path in _transcript_files(root):
        for d in _read_lines(path):
            text = _utterance_of(d)
            if text is None or text in seen:
                continue
            seen.add(text)
            yield d.get("timestamp", ""), f"{path.parent.name}/{path.stem}", text


def iter_history_utterances(path: Path) -> Iterator[tuple[str, str, str]]:
    seen: set[str] = set()
    for d in _read_lines(path):
        text = d.get("display")
        if not isinstance(text, str) or not text or text in seen or is_nonhuman_turn(text):
            continue
        seen.add(text)
        yield str(d.get("timestamp", "")), str(d.get("project", "")), text


def iter_delivered(root: Path, body: str) -> Iterator[tuple[str, str, str]]:
    """bodyが載ったUserPromptSubmit配達ごとに、親を辿って見つけた人間の発話を返す。"""
    needle = f"- {body}("
    for path in _transcript_files(root):
        by_uuid: dict[str, dict] = {}
        for d in _read_lines(path):
            if "uuid" in d:
                by_uuid[d["uuid"]] = d
        for d in by_uuid.values():
            att = d.get("attachment") or {}
            if (
                att.get("type") != "hook_additional_context"
                or att.get("hookEvent") != "UserPromptSubmit"
                or needle not in json.dumps(att.get("content"), ensure_ascii=False)
            ):
                continue
            parent = by_uuid.get(d.get("parentUuid") or "")
            while parent is not None and _utterance_of(parent) is None:
                parent = by_uuid.get(parent.get("parentUuid") or "")
            text = _utterance_of(parent) if parent else "(親の発話が見つからない)"
            yield d.get("timestamp", ""), f"{path.parent.name}/{path.stem}", text or ""


def regex_entry(regex: str) -> dict:
    """_matchesに渡せる形のutteranceエントリ（DBの行の代わり）。"""
    cond = {"tool": None, "all": [{"field": "prompt", "op": "regex", "value": regex}]}
    return {"id": 0, "name": "scan", "condition_json": json.dumps(cond)}


def match_utterances(regex: str, utterances) -> list[tuple[str, str, str]]:
    entry = regex_entry(regex)
    return [u for u in utterances if _matches(entry, timing="utterance", prompt_text=u[2])]


def _entry_body(name: str) -> str:
    conn = _connect()
    row = conn and conn.execute(
        "SELECT body FROM feedback_entries WHERE name = ? AND deleted_at IS NULL", (name,)
    ).fetchone()
    if not row:
        sys.exit(f"エントリが見つからない: {name}")
    return row["body"]


def _print(rows, width: int) -> None:
    for ts, src, text in rows:
        print(f"[{ts}] {src}\n  {text[:width].replace(chr(10), ' ')}")
    print(f"--- {len(rows)}件")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--projects", type=Path, default=CLAUDE_HOME / "projects")
    p.add_argument("--history", type=Path, default=CLAUDE_HOME / "history.jsonl")
    p.add_argument("--width", type=int, default=200, help="発話を表示する字数")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("match")
    m.add_argument("regex")
    m.add_argument("--source", choices=("transcripts", "history"), default="transcripts")
    d = sub.add_parser("delivered")
    d.add_argument("name")
    args = p.parse_args(argv)

    if args.cmd == "match":
        utterances = (
            iter_history_utterances(args.history)
            if args.source == "history"
            else iter_transcript_utterances(args.projects)
        )
        _print(match_utterances(args.regex, utterances), args.width)
    else:
        _print(list(iter_delivered(args.projects, _entry_body(args.name))), args.width)


if __name__ == "__main__":
    main()
