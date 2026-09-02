#!/usr/bin/env python3
"""transcript JSONLからAskUserQuestion呼び出しとユーザー訂正発話を抽出し、候補jsonlを吐くCLI。

DB非依存。sqlite・embeddingサーバー等の外部リソースには一切触らない。行ストリーム
（`for line in f`）で読み進めるため、巨大なtranscriptでも定数メモリで完走する。
不正な行（JSONパース不能）はスキップして処理を続行する。

使い方:
    uv run python scripts/detect_reask_candidates.py --transcript <path>
    uv run python scripts/detect_reask_candidates.py --transcript <path> --out <out.jsonl>
    uv run python scripts/detect_reask_candidates.py --transcript <path> --dict <dict.json> --max 100

出力（--out省略時はstdout）は1行1候補のJSONL:
    {"kind": "ask", "turn": 12, "text": "...", "options": [...], "context_snippet": "..."}
    {"kind": "user_correction", "turn": 23, "text": "...", "context_snippet": "..."}
    {"kind": "ask", "turn": 8, "text": "...", "context_snippet": "...", "excluded_reason": "opinion_request"}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from hooks.hook_transcript import is_user_message  # noqa: E402
from src.harness import ClaudeCodeHarness  # noqa: E402

DEFAULT_MAX_CANDIDATES = 50
CONTEXT_SNIPPET_MAX_CHARS = 200

# 訂正発話の初期辞書。skills/audit/SKILL.md の T-B1〜T-B3 発話例を初期セットとして流用する。
_CORRECTION_PHRASES = [
    r"これ前に決めなかったっけ",
    r"また同じ話してる",
    r"またこの話か",
    r"(過去の情報|自分の知っている情報).{0,10}矛盾してない",
    r"グルグル回ってない",
    r"ちゃんと過去の議論を踏まえてる",
]
DEFAULT_CORRECTION_PATTERNS = [re.compile(p) for p in _CORRECTION_PHRASES]

# 除外辞書。抽出時点で質問文がこれらのパターンに合致する場合、excluded_reasonを付与する。
# 3系統: opinion_request（Claudeへの意見・選好要求）/ user_preference_request（ユーザー自身の
# 選好・状況を尋ねる）/ environment_fact（セッション外の環境事実）。判定優先順は定義順。
# 除外は保守側（弱め）に倒し、迷ったら候補として残す。observed dataを見て拡張する前提。
_EXCLUSION_PHRASES: dict[str, list[str]] = {
    "opinion_request": [
        r"どっち",
        r"どう思う",
        r"が(いい|よい)[?？]",
    ],
    "user_preference_request": [
        r"どこまで",
        r"どれくらい",
        r"やりたい",
    ],
    # \b は日本語文字もUnicode単語構成文字として扱うため「CIは」のような和文混在では
    # 境界と判定されない。ASCII英数字のみを見る先読み/後読みで代用する。
    "environment_fact": [
        r"(?<![A-Za-z0-9])CI(?![A-Za-z0-9])",
        r"(?<![A-Za-z0-9])PR(?![A-Za-z0-9])",
        r"worktree",
        r"(?<![A-Za-z0-9])mac(os)?(?![A-Za-z0-9])",
    ],
}
DEFAULT_EXCLUSION_RULES: dict[str, list[re.Pattern]] = {
    category: [re.compile(p, re.IGNORECASE) for p in patterns]
    for category, patterns in _EXCLUSION_PHRASES.items()
}


def _normalize_text(text: str) -> str:
    """dedup比較用にtextを正規化する（前後空白除去 + 連続空白畳み込み + 小文字化）。"""
    return re.sub(r"\s+", " ", text.strip()).lower()


def _match_exclusion(text: str, exclusion_rules: dict[str, list[re.Pattern]]) -> str | None:
    for category, patterns in exclusion_rules.items():
        for pattern in patterns:
            if pattern.search(text):
                return category
    return None


def _extract_option_labels(options: object) -> list[str] | None:
    """AskUserQuestionのoptions（{label, description}の配列、または文字列配列）からlabelのみ抽出する。"""
    if not isinstance(options, list) or not options:
        return None
    labels: list[str] = []
    for opt in options:
        if isinstance(opt, dict) and opt.get("label"):
            labels.append(opt["label"])
        elif isinstance(opt, str) and opt:
            labels.append(opt)
    return labels or None


def _extract_ask_candidates(
    entry: dict, turn: int, exclusion_rules: dict[str, list[re.Pattern]]
) -> list[dict]:
    """assistantエントリからAskUserQuestion呼び出しの候補を抽出する。

    AskUserQuestionのtool_use入力は `questions` 配列を持ち、1回の呼び出しに複数の
    質問が含まれることがある（実データ確認済み）。配列内の各質問を個別の候補として
    展開する。1エントリに複数のtool_useブロックが含まれる場合も全て走査する。
    """
    message = entry.get("message", {})
    content = message.get("content", [])
    if not isinstance(content, list):
        return []

    results: list[dict] = []
    preceding_text_parts: list[str] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")

        if block_type == "text":
            text = block.get("text", "")
            if text:
                preceding_text_parts.append(text)
            continue

        if block_type != "tool_use" or "AskUserQuestion" not in block.get("name", ""):
            continue

        context_snippet = "".join(preceding_text_parts)[-CONTEXT_SNIPPET_MAX_CHARS:]
        questions = block.get("input", {}).get("questions", [])
        if not isinstance(questions, list):
            continue

        for q in questions:
            if not isinstance(q, dict):
                continue
            question_text = q.get("question")
            if not question_text or not str(question_text).strip():
                continue
            candidate: dict = {"kind": "ask", "turn": turn, "text": question_text}
            options = _extract_option_labels(q.get("options"))
            if options:
                candidate["options"] = options
            candidate["context_snippet"] = context_snippet
            reason = _match_exclusion(question_text, exclusion_rules)
            if reason:
                candidate["excluded_reason"] = reason
            results.append(candidate)

    return results


def _extract_user_text(entry: dict) -> str:
    """is_user_message判定済みのuserエントリからtextブロックのみを連結して取り出す。"""
    content = entry.get("message", {}).get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return " ".join(p for p in parts if p)
    return ""


def _extract_correction_candidate(
    entry: dict, turn: int, correction_patterns: list[re.Pattern]
) -> dict | None:
    text = _extract_user_text(entry)
    if not text.strip():
        return None
    for pattern in correction_patterns:
        if pattern.search(text):
            return {
                "kind": "user_correction",
                "turn": turn,
                "text": text,
                "context_snippet": text,
            }
    return None


def extract_candidates(
    transcript_path: str,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    correction_patterns: list[re.Pattern] | None = None,
    exclusion_rules: dict[str, list[re.Pattern]] | None = None,
) -> list[dict]:
    """transcript JSONLを行ストリームで読み、候補一覧を返す。

    turnはエントリの通し番号（0始まり、パース成功した行のみカウント）。
    同一(kind, 正規化text)の重複候補は先に見つかった方を残して1件に畳む。
    max_candidates件に達した時点でファイル読み取りを打ち切る（定数メモリの担保）。
    """
    correction_patterns = correction_patterns if correction_patterns is not None else DEFAULT_CORRECTION_PATTERNS
    exclusion_rules = exclusion_rules if exclusion_rules is not None else DEFAULT_EXCLUSION_RULES

    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    turn = 0

    def _try_add(candidate: dict) -> None:
        if len(candidates) >= max_candidates:
            return
        key = (candidate["kind"], _normalize_text(candidate["text"]))
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            if len(candidates) >= max_candidates:
                break
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")
            if entry_type == "assistant":
                for candidate in _extract_ask_candidates(entry, turn, exclusion_rules):
                    _try_add(candidate)
            elif is_user_message(ClaudeCodeHarness.to_entry(entry)):
                candidate = _extract_correction_candidate(entry, turn, correction_patterns)
                if candidate is not None:
                    _try_add(candidate)

            turn += 1

    return candidates


def format_candidates_jsonl(candidates: list[dict]) -> str:
    return "\n".join(json.dumps(c, ensure_ascii=False) for c in candidates)


def _load_dict(path: str) -> tuple[list[re.Pattern], dict[str, list[re.Pattern]]]:
    """--dictで指定されたJSONから訂正発話辞書・除外辞書を読み込む。

    キーは `correction_patterns`（正規表現文字列の配列）と
    `exclusion_patterns`（カテゴリ名 -> 正規表現文字列の配列）。
    どちらかのキーが無ければ、そちらは組み込み既定を使う。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    raw_correction = data.get("correction_patterns")
    correction_patterns = (
        [re.compile(p) for p in raw_correction] if raw_correction else DEFAULT_CORRECTION_PATTERNS
    )

    raw_exclusion = data.get("exclusion_patterns")
    exclusion_rules = (
        {category: [re.compile(p) for p in patterns] for category, patterns in raw_exclusion.items()}
        if raw_exclusion
        else DEFAULT_EXCLUSION_RULES
    )

    return correction_patterns, exclusion_rules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transcript", required=True, help="JSONL形式のtranscriptファイルパス")
    parser.add_argument("--out", default=None, help="出力先パス。省略時はstdout")
    parser.add_argument(
        "--dict",
        dest="dict_path",
        default=None,
        help="訂正発話辞書・除外辞書を上書きするJSONファイルパス。省略時は組み込み既定",
    )
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_CANDIDATES, help="抽出上限件数（既定50）")
    args = parser.parse_args(argv)

    if args.dict_path:
        correction_patterns, exclusion_rules = _load_dict(args.dict_path)
    else:
        correction_patterns, exclusion_rules = DEFAULT_CORRECTION_PATTERNS, DEFAULT_EXCLUSION_RULES

    candidates = extract_candidates(
        args.transcript,
        max_candidates=args.max,
        correction_patterns=correction_patterns,
        exclusion_rules=exclusion_rules,
    )

    output = format_candidates_jsonl(candidates)
    if args.out:
        Path(args.out).write_text(output + ("\n" if output else ""), encoding="utf-8")
    else:
        if output:
            print(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
