import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.utterance_scan import (  # noqa: E402
    iter_delivered,
    iter_history_utterances,
    iter_transcript_utterances,
    match_utterances,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")


def _user(uuid: str, content, parent: str | None = None, **extra) -> dict:
    return {"type": "user", "uuid": uuid, "parentUuid": parent, "message": {"content": content}, **extra}


def _projects(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    _write_jsonl(root / "proj" / "s1.jsonl", [
        _user("u1", "これがそろそろ出るから"),
        _user("u2", [{"type": "tool_result", "content": "これがそろそろ出る"}], "u1"),
        _user("u3", "Stop hook feedback: これがそろそろ出る", "u2", isMeta=True),
        _user("u4", "Another Claude session sent a message: これがそろそろ出る", "u3"),
        {"type": "attachment", "uuid": "a1", "parentUuid": "u4",
         "attachment": {"type": "queued_command", "prompt": "割り込み: これが寝るね"}},
        _user("u5", [{"type": "text", "text": "画像つきで結論的に"}, {"type": "image"}], "a1"),
        {"type": "attachment", "uuid": "h1", "parentUuid": "u5",
         "attachment": {"type": "hook_additional_context", "hookEvent": "UserPromptSubmit",
                        "content": ["<system-reminder>過去の躓きから学んだ注意点:\n- 結論的の対応(1回目)</system-reminder>"]}},
    ])
    _write_jsonl(root / "proj" / "s2.jsonl", [_user("v1", "これがそろそろ出るから")])
    _write_jsonl(root / "proj" / "s1" / "subagents" / "x.jsonl", [_user("w1", "これが戻る")])
    return root


def test_transcript_utterances_keep_only_human_turns_deduped(tmp_path):
    texts = [t for _, _, t in iter_transcript_utterances(_projects(tmp_path))]
    assert texts == ["これがそろそろ出るから", "割り込み: これが寝るね", "画像つきで結論的に"]


def test_match_uses_hook_regex_semantics(tmp_path):
    hits = match_utterances("これが(そろそろ)?(出る|寝る)", iter_transcript_utterances(_projects(tmp_path)))
    assert [t for _, _, t in hits] == ["これがそろそろ出るから", "割り込み: これが寝るね"]


def test_delivered_walks_parents_to_human_utterance(tmp_path):
    rows = list(iter_delivered(_projects(tmp_path), "結論的の対応"))
    assert [t for _, _, t in rows] == ["画像つきで結論的に"]
    assert list(iter_delivered(_projects(tmp_path), "別の本文")) == []


def test_history_skips_nonhuman_and_duplicates(tmp_path):
    path = tmp_path / "history.jsonl"
    _write_jsonl(path, [
        {"display": "花火みたいな花", "timestamp": 1, "project": "p"},
        {"display": "花火みたいな花", "timestamp": 2, "project": "p"},
        {"display": "<task-notification>花火みたいな花", "timestamp": 3, "project": "p"},
    ])
    assert [t for _, _, t in iter_history_utterances(path)] == ["花火みたいな花"]
