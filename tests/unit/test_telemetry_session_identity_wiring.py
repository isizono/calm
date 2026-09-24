"""search/detect_reask_candidates/get_by_ids/get_material（main.py）のtelemetry計装
session_id解決 配線のunit test。

記録=クエリ添付の追随カウンタ（injection_telemetry）は、提示側と取得側のセッションIDが
一致して初めてJOINが成立する。取得側4箇所を`_current_session_id()`（MCP接続単位の
ephemeral ID）ではなく`get_caller_session_id()`（server再起動をまたいで安定な恒久ID）に
揃える配線を検証する（test_ask_tool_session_identity_wiring.pyと同じ形）。各サービス自体の
telemetry記録ロジックはtests/unit/test_search_telemetry.py・test_injection_telemetry.pyが
担うためここでは扱わない。
"""
from unittest.mock import MagicMock

import pytest

import src.main as main_module


@pytest.fixture(autouse=True)
def _fixed_caller_session_id(monkeypatch):
    monkeypatch.setattr(main_module, "get_caller_session_id", lambda: "sess-1")


@pytest.fixture(autouse=True)
def _reject_current_session_id(monkeypatch):
    """`_current_session_id()`が使われたら即座に検出できるよう別値を返す。"""
    monkeypatch.setattr(
        main_module, "_current_session_id", lambda: "stale-ephemeral-id"
    )


class TestSearchUsesStableSessionIdentity:
    def test_passes_resolved_identity_as_caller_session_id(self, monkeypatch):
        stub = MagicMock(return_value={"results": [], "total_count": 0, "search_methods_used": []})
        monkeypatch.setattr(main_module.search_service, "search", stub)

        main_module.search(keyword="test")

        assert stub.call_args.kwargs["caller_session_id"] == "sess-1"


class TestDetectReaskCandidatesUsesStableSessionIdentity:
    def test_passes_resolved_identity_as_caller_session_id(self, monkeypatch):
        stub = MagicMock(return_value={"candidates": []})
        monkeypatch.setattr(main_module.reask_detection_service, "detect_reask_candidates", stub)

        main_module.detect_reask_candidates(transcript_path="/tmp/does-not-matter.jsonl")

        assert stub.call_args.kwargs["caller_session_id"] == "sess-1"


class TestGetByIdsUsesStableSessionIdentity:
    def test_passes_resolved_identity_as_caller_session_id(self, monkeypatch):
        stub = MagicMock(return_value={"results": []})
        monkeypatch.setattr(main_module.search_service, "get_by_ids", stub)

        main_module.get_by_ids(items=[{"type": "topic", "id": 1}])

        assert stub.call_args.kwargs["caller_session_id"] == "sess-1"


class TestGetMaterialUsesStableSessionIdentity:
    def test_passes_resolved_identity_as_caller_session_id(self, temp_db, monkeypatch):
        # get_materialのラッパーはmaterial_service.get_materialをmockしても、後段の
        # citation flavor適用（_apply_flavor_to_single）が実DBへcitationsテーブルを
        # 問い合わせる。temp_dbで隔離しないと環境依存のDBパスに実接続してしまう。
        monkeypatch.setattr(
            main_module.material_service, "get_material",
            MagicMock(return_value={"material_id": 1, "title": "t", "content": "c",
                                     "source": "s", "tags": [], "created_at": "now"}),
        )
        stub = MagicMock()
        monkeypatch.setattr(main_module.search_service, "record_material_fetch_telemetry", stub)

        main_module.get_material(material_id=1)

        assert stub.call_args.kwargs["caller_session_id"] == "sess-1"
