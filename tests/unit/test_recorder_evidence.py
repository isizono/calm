"""hooks/recorder_evidence.py の単体テスト。

入力は記録役のtranscriptに実際に書かれる形の行（tool_use・tool_result）を
ClaudeCodeHarness.to_entryで正規化したもの。
"""
import json

from hooks.recorder_evidence import missing_evidence
from src.harness.claude_code import ClaudeCodeHarness

_PREFIX = "mcp__calm__"


def _call(use_id: str, tool: str, result, *, is_error: bool = False) -> list:
    use = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": use_id, "name": _PREFIX + tool, "input": {}},
    ]}}
    lines = [use]
    if result is not None:
        block = {"type": "tool_result", "tool_use_id": use_id,
                 "content": [{"type": "text", "text": json.dumps(result)}]}
        if is_error:
            block["is_error"] = True
        lines.append({"type": "user", "message": {"content": [block]}})
    return lines


def _entries(*groups) -> list:
    return [ClaudeCodeHarness.to_entry(raw) for group in groups for raw in group]


_SEARCH_HIT = {"results": [{"type": "log", "id_raw": 120, "title": "同じ経緯"}]}


def test_search_and_write_is_enough():
    entries = _entries(_call("a", "search", _SEARCH_HIT), _call("b", "add_logs", {"created": [{"log_id": 1}]}))
    assert missing_evidence(entries, "DONE 0001") == []


def test_write_without_search_is_rejected():
    entries = _entries(_call("b", "add_material", {"material_id": 3}))
    assert missing_evidence(entries, "DONE 0001") == ["成功したsearchが無い"]


def test_search_only_without_write_or_skip_is_rejected():
    entries = _entries(_call("a", "search", _SEARCH_HIT))
    assert missing_evidence(entries, "DONE 0001") == ["記録(add_logs/add_material)もSKIP行も無い"]


def test_nothing_at_all_reports_both():
    assert missing_evidence([], "DONE 0001") == [
        "成功したsearchが無い",
        "記録(add_logs/add_material)もSKIP行も無い",
    ]


def test_skip_none_with_search_is_accepted():
    entries = _entries(_call("a", "search", {"results": []}))
    assert missing_evidence(entries, "SKIP 該当なし 雑談だけ\nDONE 0001") == []


def test_skip_none_without_reason_is_rejected():
    entries = _entries(_call("a", "search", {"results": []}))
    assert missing_evidence(entries, "SKIP 該当なし\nDONE 0001") == ["記録(add_logs/add_material)もSKIP行も無い"]


def test_skip_dup_naming_a_searched_id_is_accepted():
    entries = _entries(_call("a", "search", _SEARCH_HIT))
    assert missing_evidence(entries, "SKIP 重複 log:120 メインが記録済み\nDONE 0001") == []


def test_skip_dup_with_id_not_in_search_results_is_rejected():
    entries = _entries(_call("a", "search", _SEARCH_HIT))
    assert missing_evidence(entries, "SKIP 重複 log:12 メインが記録済み\nDONE 0001") == [
        "SKIP 重複の重複先 `log:12` がこの片のsearch結果に無い"
    ]


def test_skip_dup_checks_every_target():
    entries = _entries(_call("a", "search", _SEARCH_HIT))
    assert missing_evidence(entries, "SKIP 重複 log:120,material:45 同じ\nDONE 0001") == [
        "SKIP 重複の重複先 `material:45` がこの片のsearch結果に無い"
    ]


def test_skip_dup_with_malformed_target_is_rejected():
    entries = _entries(_call("a", "search", _SEARCH_HIT))
    assert missing_evidence(entries, "SKIP 重複 #120 同じ\nDONE 0001") == [
        "SKIP 重複の重複先 `#120` が <種別>:<id> の形でない"
    ]


def test_failed_calls_do_not_count():
    entries = _entries(
        _call("a", "search", {"error": {"code": "DATABASE_ERROR"}}),
        _call("b", "add_logs", "denied", is_error=True),
        _call("c", "add_material", None),  # 拒否されて結果が付かなかった
    )
    assert missing_evidence(entries, "DONE 0001") == [
        "成功したsearchが無い",
        "記録(add_logs/add_material)もSKIP行も無い",
    ]


def test_add_relation_alone_is_not_a_record():
    entries = _entries(_call("a", "search", _SEARCH_HIT), _call("b", "add_relation", {"created": 1}))
    assert missing_evidence(entries, "DONE 0001") == ["記録(add_logs/add_material)もSKIP行も無い"]


def test_plugin_prefixed_tool_names_are_recognized():
    entries = _entries(_call("a", "search", _SEARCH_HIT), _call("b", "add_logs", {"created": []}))
    for line in entries:
        for block in line.content:
            if block.get("type") == "tool_use":
                block["name"] = block["name"].replace(_PREFIX, "mcp__plugin_calm_calm__")
    assert missing_evidence(entries, "DONE 0001") == []
