"""launcherのtools/listフィルタのユニットテスト

エッジケース:
- CCM_OW未設定&OW_ROLE未設定 → tools/list応答からow_*を除外
- CCM_OW=1 → ow_*を含めた全ツールを返す
- OW_ROLE=worker → ow_*を含めた全ツールを返す
- ow_*以外のツールは常に全て返す
"""
import json
import os
import sys
from typing import Any
from unittest.mock import patch

import pytest


def _reload_launcher_with_env(env_vars: dict) -> Any:
    """環境変数を設定してlauncher.pyを再ロードする"""
    env_patch: dict = {"CC_MEMORY_URL": ""}
    for key in ("CCM_OW", "OW_ROLE"):
        env_patch[key] = ""  # デフォルトは空（未設定相当）
    env_patch.update(env_vars)

    with patch.dict(os.environ, env_patch):
        if "src.launcher" in sys.modules:
            del sys.modules["src.launcher"]
        import src.launcher as launcher
        return launcher


ALL_TOOLS = [
    {"name": "search", "description": "検索"},
    {"name": "add_topic", "description": "トピック追加"},
    {"name": "ow_send", "description": "OW送信"},
    {"name": "ow_history", "description": "OW履歴"},
    {"name": "ow_spawn_worker", "description": "OWワーカー起動"},
    {"name": "ow_close_worker", "description": "OWワーカー終了"},
    {"name": "ow_status", "description": "OWステータス"},
]

NON_OW_TOOLS = [t for t in ALL_TOOLS if not t["name"].startswith("ow_")]
OW_TOOLS = [t for t in ALL_TOOLS if t["name"].startswith("ow_")]


def _make_response(tools: list, resp_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": resp_id, "result": {"tools": tools}}


def _apply_filter(m: Any, response: dict, req_id: int) -> dict:
    """ブリッジロジックのフィルタ部分を再現する"""
    json_bytes = json.dumps(response).encode("utf-8")

    if not m._OW_ENABLED and m._pending_tools_list_ids:
        try:
            parsed = json.loads(json_bytes)
            rid = parsed.get("id")
            if rid in m._pending_tools_list_ids:
                m._pending_tools_list_ids.discard(rid)
                tools = parsed.get("result", {}).get("tools", [])
                if tools:
                    filtered = [t for t in tools if not t.get("name", "").startswith("ow_")]
                    parsed["result"]["tools"] = filtered
                    json_bytes = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
        except (json.JSONDecodeError, AttributeError, KeyError):
            pass

    return json.loads(json_bytes)


class TestOwEnabled:
    """CCM_OW=1またはOW_ROLE=workerの場合はow_*を含めた全ツールを返す"""

    def test_ccm_ow_1_is_enabled(self):
        """CCM_OW=1のとき _OW_ENABLED=True"""
        launcher = _reload_launcher_with_env({"CCM_OW": "1"})
        assert launcher._OW_ENABLED is True

    def test_ow_role_worker_is_enabled(self):
        """OW_ROLE=workerのとき _OW_ENABLED=True"""
        launcher = _reload_launcher_with_env({"OW_ROLE": "worker"})
        assert launcher._OW_ENABLED is True

    def test_ow_role_orch_is_disabled(self):
        """OW_ROLE=orch など worker以外は _OW_ENABLED=False"""
        launcher = _reload_launcher_with_env({"OW_ROLE": "orch"})
        assert launcher._OW_ENABLED is False

    def test_ow_role_empty_is_disabled(self):
        """OW_ROLE=（空文字）は _OW_ENABLED=False"""
        launcher = _reload_launcher_with_env({"OW_ROLE": ""})
        assert launcher._OW_ENABLED is False


class TestOwDisabled:
    """CCM_OW未設定&OW_ROLE未設定の場合はow_*を除外"""

    def test_no_env_is_disabled(self):
        """環境変数なしのとき _OW_ENABLED=False"""
        launcher = _reload_launcher_with_env({})
        assert launcher._OW_ENABLED is False

    def test_ccm_ow_0_is_disabled(self):
        """CCM_OW=0のとき _OW_ENABLED=False"""
        launcher = _reload_launcher_with_env({"CCM_OW": "0"})
        assert launcher._OW_ENABLED is False


class TestFilterLogic:
    """tools/listレスポンスのフィルタロジックを検証する"""

    def test_disabled_filters_ow_tools(self):
        """OW無効時はtools/listレスポンスからow_*を除外する"""
        m = _reload_launcher_with_env({})
        assert m._OW_ENABLED is False
        m._pending_tools_list_ids.add(42)
        result = _apply_filter(m, _make_response(ALL_TOOLS, 42), 42)
        names = [t["name"] for t in result["result"]["tools"]]
        for t in OW_TOOLS:
            assert t["name"] not in names
        for t in NON_OW_TOOLS:
            assert t["name"] in names

    def test_enabled_returns_all_tools(self):
        """OW有効時はow_*を含めた全ツールを返す（フィルタしない）"""
        m = _reload_launcher_with_env({"CCM_OW": "1"})
        assert m._OW_ENABLED is True
        # OW有効時はIDをトラッキングしないのでpending_idsは空のまま
        result = _apply_filter(m, _make_response(ALL_TOOLS, 43), 43)
        names = [t["name"] for t in result["result"]["tools"]]
        for t in ALL_TOOLS:
            assert t["name"] in names

    def test_non_ow_tools_always_returned(self):
        """ow_*以外のツールは常に全て返す"""
        m = _reload_launcher_with_env({})
        assert m._OW_ENABLED is False
        m._pending_tools_list_ids.add(44)
        result = _apply_filter(m, _make_response(NON_OW_TOOLS, 44), 44)
        names = [t["name"] for t in result["result"]["tools"]]
        for t in NON_OW_TOOLS:
            assert t["name"] in names

    def test_untracked_id_not_filtered(self):
        """トラッキングされていないIDのレスポンスはフィルタしない"""
        m = _reload_launcher_with_env({})
        assert m._OW_ENABLED is False
        m._pending_tools_list_ids.clear()
        result = _apply_filter(m, _make_response(ALL_TOOLS, 99), 99)
        names = [t["name"] for t in result["result"]["tools"]]
        for t in OW_TOOLS:
            assert t["name"] in names

    def test_id_discarded_after_filter(self):
        """フィルタ後はpending_tools_list_idsからIDが除去される"""
        m = _reload_launcher_with_env({})
        m._pending_tools_list_ids.add(55)
        _apply_filter(m, _make_response(ALL_TOOLS, 55), 55)
        assert 55 not in m._pending_tools_list_ids

    def test_ow_role_worker_returns_all_tools(self):
        """OW_ROLE=workerのときもow_*を含めた全ツールを返す"""
        m = _reload_launcher_with_env({"OW_ROLE": "worker"})
        assert m._OW_ENABLED is True
        result = _apply_filter(m, _make_response(ALL_TOOLS, 77), 77)
        names = [t["name"] for t in result["result"]["tools"]]
        for t in ALL_TOOLS:
            assert t["name"] in names
