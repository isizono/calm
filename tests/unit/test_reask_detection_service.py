"""reask_detection_service.detect_reask_candidates のテスト。

transcript pathの解決失敗、excluded_reason付き候補の除外、search_top_nによる
候補打ち切り、既存記録との類似search結果の反映を検証する。
"""
import json
import os
import tempfile

import pytest

import src.services.embedding_service as emb
from src.db import init_database
from src.services import reask_detection_service
from src.services.topic_service import add_topic


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    monkeypatch.setattr(emb, "_server_initialized", False)
    monkeypatch.setattr(emb, "_backfill_done", True)
    monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _write_transcript(tmpdir: str, entries: list[dict]) -> str:
    path = os.path.join(tmpdir, "transcript.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def _ask_entry(question: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "AskUserQuestion",
                    "input": {"questions": [{"question": question}]},
                }
            ]
        },
    }


def test_transcript_not_found_returns_error(temp_db):
    result = reask_detection_service.detect_reask_candidates("/no/such/transcript.jsonl")

    assert result["error"]["code"] == "TRANSCRIPT_NOT_FOUND"


def test_excluded_candidate_is_filtered_and_not_searched(temp_db, monkeypatch):
    """opinion_request等のexcluded_reason付き候補はcandidatesに出ず、searchも呼ばれない。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_transcript(tmpdir, [_ask_entry("案Aと案Bどっちがいいと思う?")])

        search_calls = []
        from src.services import search_service

        real_search = search_service.search

        def tracking_search(*args, **kwargs):
            search_calls.append((args, kwargs))
            return real_search(*args, **kwargs)

        monkeypatch.setattr(reask_detection_service.search_service, "search", tracking_search)

        result = reask_detection_service.detect_reask_candidates(path)

    assert result["total_extracted"] == 1
    assert result["excluded_count"] == 1
    assert result["candidates"] == []
    assert result["searched_count"] == 0
    assert search_calls == []


def test_high_similarity_hit_is_attached_to_candidate(temp_db):
    """既存recordとテキストが一致する候補はtop_hitsにそのrecordを含む。

    embeddingサーバーを無効化しているためFTS5(trigram)のみが働く。trigramは
    実質的な部分文字列一致のため、候補textを既存recordの本文に含まれる
    部分文字列と完全一致させてヒットを作る。
    """
    add_topic(
        title="uplinkのretry上限",
        description="uplinkのretry上限は何回に固定するかで議論した結果、3回に決まった",
        tags=["domain:cc-memory-test-reask", "intent:discuss"],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_transcript(
            tmpdir, [_ask_entry("uplinkのretry上限は何回に固定するか")]
        )
        result = reask_detection_service.detect_reask_candidates(path, score_threshold=0.0)

    assert result["searched_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["kind"] == "ask"
    hit_titles = [h["title"] for h in candidate["top_hits"]]
    assert any("uplink" in (t or "") for t in hit_titles)


def test_search_top_n_truncates_candidates(temp_db):
    """excluded_reasonの無い候補がsearch_top_nを超える場合、超過分はsearch対象外になる。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        entries = [_ask_entry(f"これは候補{i}についての確認事項です") for i in range(5)]
        path = _write_transcript(tmpdir, entries)
        result = reask_detection_service.detect_reask_candidates(path, search_top_n=2)

    assert result["total_extracted"] == 5
    assert result["excluded_count"] == 0
    assert result["searched_count"] == 2
    assert result["truncated_count"] == 3
    assert len(result["candidates"]) == 2


def test_max_candidates_limits_extraction(temp_db):
    """max_candidatesで抽出段階そのものが打ち切られる。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        entries = [_ask_entry(f"これは候補{i}についての確認事項です") for i in range(5)]
        path = _write_transcript(tmpdir, entries)
        result = reask_detection_service.detect_reask_candidates(path, max_candidates=2, search_top_n=10)

    assert result["total_extracted"] == 2
