#!/usr/bin/env python3
"""orchの担い手・相談役が、世代交代の契機を超えたかを判定する。

決定「担い手の交代は測れる契機で、測るのは相手側」の3条件を、transcriptと
セッション開始時刻(transcript最初の行のtimestamp)から判定し、1行のJSONで
stdoutへ出す。

- 文脈: transcriptの最後のassistantのusageで、input_tokens・
  cache_read_input_tokens・cache_creation_input_tokensの合計が
  --thresholdを超えた
- 圧縮: isCompactSummaryを持つ行が1つでもあった
- skillの版: --skill-pathのmtimeが、セッション開始時刻より後。相談役
  (--role consultant)には当てない

skillの版の判定にmtimeを使う(決定の文言は「git logで判定」だが、labの
未コミット変更も、gitで管理されていないインストール版のプラグインキャッシュも
同じロジックで捕捉できるmtime比較を実装上の判断として採る)。

標準ライブラリのみに依存する。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.orch_liveness import find_transcript  # noqa: E402

_DEFAULT_THRESHOLD = 330_000
_DEFAULT_SKILL_PATH = _project_root / "skills" / "orch" / "SKILL.md"


def _parse_ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _context_tokens_from_usage(usage: dict) -> int:
    """1件のassistant行のusageから、今のコンテキストサイズを計算する。

    advisor等でassistant行の中に複数回のAPI呼び出し(iterations)が束ねられて
    いるとき、トップレベルのusageはiterationsの合計になり、実際のコンテキスト
    サイズの約2倍に見える(資材「引き継ぎ区間の読み込み内訳(10/09)」で実測)。
    そのためiterationsがあれば、最後のtype="message"の要素を使う。
    """
    iterations = usage.get("iterations")
    if iterations:
        message_iters = [it for it in iterations if it.get("type") == "message"]
        source = (message_iters or iterations)[-1]
    else:
        source = usage
    return (
        (source.get("input_tokens") or 0)
        + (source.get("cache_read_input_tokens") or 0)
        + (source.get("cache_creation_input_tokens") or 0)
    )


def judge(lines: list[dict], *, role: str, threshold: int,
          skill_mtime: float | None) -> dict:
    """transcriptの行(パース済み辞書)列から交代の契機を判定する。"""
    context_tokens: int | None = None
    compacted = False
    session_start: float | None = None
    for obj in lines:
        if session_start is None:
            ts = obj.get("timestamp")
            if ts:
                try:
                    session_start = _parse_ts(ts)
                except ValueError:
                    pass
        if obj.get("isCompactSummary"):
            compacted = True
        if obj.get("type") == "assistant":
            usage = (obj.get("message") or {}).get("usage")
            if usage:
                context_tokens = _context_tokens_from_usage(usage)

    reasons = []
    if context_tokens is not None and context_tokens > threshold:
        reasons.append("context")
    if compacted:
        reasons.append("compact")

    skill_changed: bool | None = None
    if role != "consultant":
        skill_changed = bool(
            skill_mtime is not None and session_start is not None
            and skill_mtime > session_start
        )
        if skill_changed:
            reasons.append("skill_version")

    return {
        "trigger": bool(reasons),
        "reasons": reasons,
        "context_tokens": context_tokens,
        "compacted": compacted,
        "skill_changed": skill_changed,
        "session_start": session_start,
    }


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--session-id", help="担い手(または相談役)のsessionId")
    parser.add_argument("--transcript", help="transcriptのパス(--session-idの代わりに直接指定)")
    parser.add_argument(
        "--role", choices=["holder", "consultant"], default="holder",
        help="skillの版の契機を当てる対象かどうか(既定: holder。相談役には当てない)",
    )
    parser.add_argument("--threshold", type=int, default=_DEFAULT_THRESHOLD,
                        help=f"文脈の契機の閾値(既定: {_DEFAULT_THRESHOLD})")
    parser.add_argument("--skill-path", default=str(_DEFAULT_SKILL_PATH),
                        help="版の変化を見るSKILL.mdのパス")
    parser.add_argument("--projects-dir", default=str(Path.home() / ".claude" / "projects"),
                        help="--session-id指定時にtranscriptを探すディレクトリ")
    args = parser.parse_args(argv)

    if not args.transcript and not args.session_id:
        parser.error("--session-idまたは--transcriptのいずれかが必要")
        return

    if args.transcript:
        transcript = Path(args.transcript)
    else:
        transcript = find_transcript(args.session_id, Path(args.projects_dir))

    if transcript is None or not transcript.is_file():
        print(json.dumps({"error": "transcript not found"}, ensure_ascii=False))
        return

    skill_path = Path(args.skill_path)
    skill_mtime = skill_path.stat().st_mtime if skill_path.is_file() else None

    result = judge(_read_jsonl(transcript), role=args.role, threshold=args.threshold,
                   skill_mtime=skill_mtime)
    result["transcript"] = str(transcript)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
