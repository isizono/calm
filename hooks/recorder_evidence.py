"""記録役が片ごとに残すべき記録証跡の判定。

片のDONEを受け付けてよいかを、記録役自身のtranscript（片を渡した後の差分）と
最終メッセージだけから決める純粋関数群。見張り（hooks/recorder_watch.py）の
DONE確定と、回帰ハーネス（scripts/recorder_harness.py）の検証の両方が使う。

受け付ける形は次のどちらか。どちらでも、その片の中で成功したsearchが1回以上
要る（重複を確かめずに書いた・飛ばした片を区別できなくなるため）。

- 記録した: add_logs・add_materialのどれかが成功している
- 飛ばした: 最終メッセージに次のどちらかのSKIP行がある
  - `SKIP 重複 <種別>:<id>[,<種別>:<id>…] <理由>` 。idはその片で成功した
    searchの結果に現れたものに限る（存在しない重複先を書いて飛ばすのを防ぐ）
  - `SKIP 該当なし <理由>` 。記録すべき出来事が無い片のため
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from hooks.hook_transcript import _extract_short_name, _is_calm_tool
from src.harness.interface import TranscriptEntry

SHARD_PROMPT_RE = re.compile(r"片(\d+)を処理せよ")
NOOP_PROMPT_MARK = "片なし"
DONE_RE = re.compile(r"\bDONE\s+(\d+)\b")

_SEARCH_TOOLS = {"search"}
_WRITE_TOOLS = {"add_logs", "add_material"}

# 空白は行内に限る（`\s`だと改行を越えて次の行のDONEを理由と取り違える）。
_SP = r"[ \t\u3000]"
_SKIP_DUP_RE = re.compile(rf"^{_SP}*SKIP{_SP}+重複{_SP}+(\S+){_SP}+\S", re.MULTILINE)
_SKIP_NONE_RE = re.compile(rf"^{_SP}*SKIP{_SP}+該当なし{_SP}+\S", re.MULTILINE)
_DUP_TARGET_RE = re.compile(r"^(log|material|decision|topic|activity):(\d+)$")


@dataclass
class CalmCall:
    short_name: str
    ok: bool
    result_text: str


def _result_text(block: dict) -> str:
    content = block.get("content", "")
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content)


def _result_ok(block: dict, text: str) -> bool:
    if block.get("is_error"):
        return False
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return True
    return not (isinstance(data, dict) and "error" in data)


def calm_calls(entries: list[TranscriptEntry]) -> list[CalmCall]:
    """CALMツールの呼び出しを、結果と突き合わせて呼び出し順に返す。

    結果が無い呼び出し（拒否されて結果が付かなかった等）はok=Falseにする。
    同じidのtool_useは1回に数える。
    """
    order: list[str] = []
    names: dict[str, str] = {}
    results: dict[str, dict] = {}
    for entry in entries:
        for block in entry.content:
            btype = block.get("type")
            if btype == "tool_use":
                name = block.get("name", "")
                use_id = block.get("id")
                # transcriptには同じtool_useが2回書かれることがあるのでidで畳む
                if use_id and use_id not in names and _is_calm_tool(name):
                    order.append(use_id)
                    names[use_id] = _extract_short_name(name)
            elif btype == "tool_result":
                use_id = block.get("tool_use_id")
                if use_id in names:
                    results[use_id] = block
    calls = []
    for use_id in order:
        block = results.get(use_id)
        if block is None:
            calls.append(CalmCall(names[use_id], False, ""))
            continue
        text = _result_text(block)
        calls.append(CalmCall(names[use_id], _result_ok(block, text), text))
    return calls


def _skip_problem(last_msg: str, search_texts: list[str]) -> str | None:
    """SKIP行が正しければNone、無い・不正なら理由を返す。"""
    if _SKIP_NONE_RE.search(last_msg):
        return None
    m = _SKIP_DUP_RE.search(last_msg)
    if m is None:
        return "記録(add_logs/add_material)もSKIP行も無い"
    targets = [t for t in re.split(r"[,、]", m.group(1)) if t]
    joined = "\n".join(search_texts)
    for target in targets:
        tm = _DUP_TARGET_RE.match(target)
        if tm is None:
            return f"SKIP 重複の重複先 `{target}` が <種別>:<id> の形でない"
        if not re.search(rf"(?<!\d){tm.group(2)}(?!\d)", joined):
            return f"SKIP 重複の重複先 `{target}` がこの片のsearch結果に無い"
    return None


def missing_evidence(entries: list[TranscriptEntry], last_msg: str) -> list[str]:
    """片の記録証跡で足りないものを返す。空ならDONEを受け付けてよい。"""
    calls = calm_calls(entries)
    searches = [c.result_text for c in calls if c.ok and c.short_name in _SEARCH_TOOLS]
    missing = []
    if not searches:
        missing.append("成功したsearchが無い")
    if not any(c.ok and c.short_name in _WRITE_TOOLS for c in calls):
        problem = _skip_problem(last_msg, searches)
        if problem:
            missing.append(problem)
    return missing
