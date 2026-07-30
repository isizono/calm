"""聞き返し検出の機械部分を1回のサービス呼び出しに集約する。

transcript path解決済みの前提で、候補抽出（scripts/detect_reask_candidates.py）→
excluded_reason付き候補の除外→残候補上位N件のsearchバッチ実行、までを行う。
既存記録との類似度に基づく「聞き返しが不要だったか」の主観判定と
report_signal(kind="precedent_miss")の呼び出しは、本サービスの範囲外のまま
呼び出し側（skills/sync-memory/SKILL.md ステップ9）に残す。
"""
from pathlib import Path
from typing import Optional

from scripts.detect_reask_candidates import extract_candidates
from src.services import search_service

DEFAULT_MAX_CANDIDATES = 50
DEFAULT_SEARCH_TOP_N = 8
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_SCORE_THRESHOLD = 0.4


def detect_reask_candidates(
    transcript_path: str,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    search_top_n: int = DEFAULT_SEARCH_TOP_N,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    caller_session_id: Optional[str] = None,
) -> dict:
    """transcriptから聞き返し候補を抽出し、上位N件について既存記録の類似検索まで行う。

    Args:
        transcript_path: transcript JSONLのパス（`~`展開に対応）
        max_candidates: 抽出段階の上限件数
        search_top_n: search実行対象とする候補の上限件数（excluded_reason付きを除いた後の先頭N件）
        search_limit: 候補1件あたりのsearch呼び出しのlimit
        score_threshold: `candidates[].top_hits` に残す最小final_score
        caller_session_id: search呼び出しのtelemetry相関キー

    Returns:
        成功時: {
            "candidates": [
                {..extract_candidatesの各キー.., "top_hits": [{"type","id","score","title"}, ...],
                 "degraded": bool}, ...
            ],  # search対象外（excluded_reason付き、またはsearch_top_n超過）は含まない
            "total_extracted": int,  # 抽出段階の全候補数（除外分含む）
            "excluded_count": int,   # excluded_reason付きで除外した件数
            "searched_count": int,   # 実際にsearchした件数
            "truncated_count": int,  # search_top_nを超えてsearch対象外になった件数
            "degraded": bool,        # いずれかのsearch呼び出しでdegraded=Trueだったか
            "score_threshold": float,
        }
        失敗時: {"error": {"code": "TRANSCRIPT_NOT_FOUND", "message": str}}
    """
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return {
            "error": {
                "code": "TRANSCRIPT_NOT_FOUND",
                "message": f"transcript_pathが見つからない: {transcript_path}",
            }
        }

    all_candidates = extract_candidates(str(path), max_candidates=max_candidates)
    total_extracted = len(all_candidates)

    eligible = [c for c in all_candidates if "excluded_reason" not in c]
    excluded_count = total_extracted - len(eligible)

    to_search = eligible[:search_top_n]
    truncated_count = len(eligible) - len(to_search)

    degraded_any = False
    results: list[dict] = []
    for candidate in to_search:
        search_result = search_service.search(
            candidate["text"], limit=search_limit, caller_session_id=caller_session_id,
        )
        entry = dict(candidate)
        if "error" in search_result:
            entry["search_error"] = search_result["error"]
            is_degraded = bool(search_result.get("degraded"))
            degraded_any = degraded_any or is_degraded
            entry["degraded"] = is_degraded
            entry["top_hits"] = []
            results.append(entry)
            continue

        is_degraded = bool(search_result.get("degraded"))
        degraded_any = degraded_any or is_degraded
        entry["degraded"] = is_degraded
        entry["top_hits"] = [
            {
                "type": hit.get("type"),
                # search_serviceを直接呼ぶため、MCPツール層のcitation変換（id_raw -> id）を
                # 経由しない。生のidはid_rawキーに入る（main.pyのsearchツール経由の
                # レスポンスとはこの1点だけ形が異なる）。
                "id": hit.get("id_raw", hit.get("id")),
                "score": hit.get("final_score", hit.get("score")),
                "title": hit.get("title") or hit.get("snippet"),
            }
            for hit in search_result.get("results", [])
            if (hit.get("final_score", hit.get("score", 0)) or 0) >= score_threshold
        ]
        results.append(entry)

    return {
        "candidates": results,
        "total_extracted": total_extracted,
        "excluded_count": excluded_count,
        "searched_count": len(to_search),
        "truncated_count": truncated_count,
        "degraded": degraded_any,
        "score_threshold": score_threshold,
    }
