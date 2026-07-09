"""search() orchestrator のステージ駆動構造のテスト。

各ステージ関数 (_validate / _normalize / _expand / _retrieve / _merge / _rerank /
_demote_archived / _slice / _decorate) を monkeypatch で差し替えて、orchestrator が
期待される順序で呼び出し、戻り値を組み立てることを検証する。

DB を立ち上げないことでオーケストレータ単独の挙動 (ステージ呼び出し順序、
戻り値合成、_SearchEarlyReturn のキャッチ、例外時の DATABASE_ERROR 化) を
集中して確認する。
"""
from unittest.mock import MagicMock, patch

import pytest

from src.services import search_service
from src.services.search_service import _SearchEarlyReturn
from tests.helpers import make_search_context as _make_ctx


@pytest.fixture
def stub_stages(monkeypatch):
    """orchestrator の全ステージ + DB / telemetry を MagicMock で差し替える fixture。

    Returns:
        各ステージの mock dict。テスト側は call_args / return_value を操作できる。
    """
    ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",))
    sliced_results = [{"type": "topic", "id": 1, "title": "hello"}]
    merged_results = [{"type": "topic", "id": 1, "title": "hello", "score": 0.5}]
    retrieval = {"fts": [], "vec": None, "tag": [], "methods_used": ["fts5"]}
    nearby_tags = [{"tag": "x", "co_count": 3}]

    stubs = {
        "_validate": MagicMock(return_value=(["alpha"], None, None, None, None)),
        "_normalize": MagicMock(return_value=(ctx, None, None)),
        "_expand": MagicMock(return_value=ctx),
        "_retrieve": MagicMock(return_value=retrieval),
        "_merge": MagicMock(return_value=merged_results),
        "_rerank": MagicMock(return_value=merged_results),
        "_demote_archived": MagicMock(return_value=merged_results),
        "_slice": MagicMock(return_value=(sliced_results, 1)),
        "_decorate": MagicMock(return_value=(sliced_results, nearby_tags)),
        "get_connection": MagicMock(return_value=MagicMock()),
        "_record_search_telemetry_async": MagicMock(return_value=None),
    }
    for name, mock in stubs.items():
        monkeypatch.setattr(search_service, name, mock)
    return stubs


def test_orchestrator_runs_stages_in_order(stub_stages):
    """search() は _validate → _normalize → _expand → _retrieve → _merge → _rerank → _demote_archived → _slice → _decorate の順で呼び、レスポンス dict を組み立てる。"""
    result = search_service.search(keyword="alpha")

    # 9 ステージはすべて 1 回ずつ呼ばれる
    for name in [
        "_validate", "_normalize", "_expand", "_retrieve",
        "_merge", "_rerank", "_demote_archived", "_slice", "_decorate",
    ]:
        assert stub_stages[name].call_count == 1, f"{name} not called once"

    # レスポンス dict のキーは契約通り
    assert set(result.keys()) == {"results", "total_count", "search_methods_used", "degraded", "nearby_tags"}
    assert result["total_count"] == 1
    assert result["search_methods_used"] == ["fts5"]
    # stub_stages の _retrieve は vec=None を返すため degraded は True
    assert result["degraded"] is True
    assert result["nearby_tags"] == [{"tag": "x", "co_count": 3}]


def test_orchestrator_opens_conn_once_and_closes_it(stub_stages):
    """search() は orchestrator で 1 度だけ get_connection() を呼び、最後に close する。"""
    conn_mock = stub_stages["get_connection"].return_value

    search_service.search(keyword="alpha")

    assert stub_stages["get_connection"].call_count == 1
    assert conn_mock.close.call_count == 1


def test_orchestrator_passes_shared_conn_to_retrieve_and_normalize(stub_stages):
    """同じ conn インスタンスが _normalize と _retrieve に渡される。"""
    conn_mock = stub_stages["get_connection"].return_value

    search_service.search(keyword="alpha")

    # _normalize の最後の引数が conn
    normalize_call = stub_stages["_normalize"].call_args
    assert normalize_call.args[-1] is conn_mock

    # _retrieve の 2 番目の引数が conn
    retrieve_call = stub_stages["_retrieve"].call_args
    assert retrieve_call.args[1] is conn_mock


def test_orchestrator_calls_telemetry_with_result_count(stub_stages):
    """telemetry は total_count を result_count として渡される。"""
    search_service.search(keyword="alpha")

    telem_call = stub_stages["_record_search_telemetry_async"].call_args
    assert telem_call.kwargs["result_count"] == 1


def test_orchestrator_early_return_from_validate_is_caught(monkeypatch):
    """_validate が _SearchEarlyReturn を投げると、その response がそのまま返る。"""
    expected = {"error": {"code": "INVALID_KEYWORD_MODE", "message": "x"}}
    monkeypatch.setattr(
        search_service,
        "_validate",
        MagicMock(side_effect=_SearchEarlyReturn(expected)),
    )
    # validate より後ろのステージは呼ばれてはならない
    get_conn = MagicMock()
    monkeypatch.setattr(search_service, "get_connection", get_conn)

    result = search_service.search(keyword="alpha", keyword_mode="bogus")

    assert result == expected
    assert get_conn.call_count == 0


def test_orchestrator_early_return_from_normalize_closes_conn(stub_stages):
    """_normalize の _SearchEarlyReturn でも conn は close され、レスポンスが返る。"""
    early = {"results": [], "total_count": 0, "search_methods_used": []}
    stub_stages["_normalize"].side_effect = _SearchEarlyReturn(early)
    conn_mock = stub_stages["get_connection"].return_value

    result = search_service.search(keyword="alpha")

    assert result == early
    assert conn_mock.close.call_count == 1
    # 後続ステージは呼ばれない
    assert stub_stages["_expand"].call_count == 0
    assert stub_stages["_retrieve"].call_count == 0


def test_orchestrator_early_return_from_retrieve_is_caught(stub_stages):
    """_retrieve が _SearchEarlyReturn (KEYWORD_TOO_SHORT) を投げてもレスポンスが返る。"""
    early = {"error": {"code": "KEYWORD_TOO_SHORT", "message": "x"}}
    stub_stages["_retrieve"].side_effect = _SearchEarlyReturn(early)

    result = search_service.search(keyword="alpha")

    assert result == early
    # _merge 以降は呼ばれない
    assert stub_stages["_merge"].call_count == 0
    assert stub_stages["_rerank"].call_count == 0


def test_orchestrator_unexpected_exception_returns_database_error(stub_stages):
    """ステージ内で予期しない例外が発生したら DATABASE_ERROR にラップされる。"""
    stub_stages["_retrieve"].side_effect = RuntimeError("boom")

    result = search_service.search(keyword="alpha")

    assert "error" in result
    assert result["error"]["code"] == "DATABASE_ERROR"
    assert "boom" in result["error"]["message"]
