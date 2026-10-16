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


def test_write_is_enough_without_search():
    entries = _entries(_call("b", "add_material", {"material_id": 3}))
    assert missing_evidence(entries, "DONE 0001") == []


def test_search_and_write_is_enough():
    entries = _entries(_call("a", "search", _SEARCH_HIT), _call("b", "add_logs", {"created": [{"log_id": 1}]}))
    assert missing_evidence(entries, "DONE 0001") == []


def test_search_only_without_write_or_skip_is_rejected():
    entries = _entries(_call("a", "search", _SEARCH_HIT))
    assert missing_evidence(entries, "DONE 0001") == ["記録(add_logs/add_material)もSKIP行も無い"]


def test_nothing_at_all_is_rejected():
    assert missing_evidence([], "DONE 0001") == ["記録(add_logs/add_material)もSKIP行も無い"]


def test_skip_none_without_search_is_accepted():
    assert missing_evidence([], "SKIP 記録対象なし 雑談だけ\nDONE 0001") == []


def test_skip_none_without_reason_is_rejected():
    assert missing_evidence([], "SKIP 記録対象なし\nDONE 0001") == ["記録(add_logs/add_material)もSKIP行も無い"]


def test_skip_dup_is_accepted():
    assert missing_evidence([], "SKIP 重複 log:120,material:45 メインが記録済み\nDONE 0001") == []


def test_skip_dup_without_reason_is_rejected():
    assert missing_evidence([], "SKIP 重複 log:120\nDONE 0001") == ["記録(add_logs/add_material)もSKIP行も無い"]


def test_skip_dup_with_malformed_target_is_rejected():
    assert missing_evidence([], "SKIP 重複 log:120,#45 同じ\nDONE 0001") == [
        "SKIP 重複の重複先 `#45` が <種別>:<id> の形でない"
    ]


def test_free_text_skip_reason_is_not_a_skip_line():
    """決まった書式でない本文の理由は、機械で拾えないので証跡にならない。"""
    assert missing_evidence([], "既に記録済みなのでスキップ\nDONE 0001") == ["記録(add_logs/add_material)もSKIP行も無い"]


def test_failed_writes_do_not_count():
    entries = _entries(
        _call("a", "add_logs", {"error": {"code": "VALIDATION_ERROR"}}),
        _call("b", "add_logs", "denied", is_error=True),
        _call("c", "add_material", None),  # 拒否されて結果が付かなかった
    )
    assert missing_evidence(entries, "DONE 0001") == ["記録(add_logs/add_material)もSKIP行も無い"]


def test_add_relation_alone_is_not_a_record():
    entries = _entries(_call("b", "add_relation", {"created": 1}))
    assert missing_evidence(entries, "DONE 0001") == ["記録(add_logs/add_material)もSKIP行も無い"]


def test_plugin_prefixed_tool_names_are_recognized():
    entries = _entries(_call("b", "add_logs", {"created": []}))
    for line in entries:
        for block in line.content:
            if block.get("type") == "tool_use":
                block["name"] = block["name"].replace(_PREFIX, "mcp__plugin_calm_calm__")
    assert missing_evidence(entries, "DONE 0001") == []


def test_tool_use_written_twice_is_counted_once():
    """実transcriptでは同じtool_useが2行に書かれることがある。"""
    from hooks.recorder_evidence import calm_calls

    search = _call("a", "search", _SEARCH_HIT)
    entries = _entries([search[0]], search)
    assert [c.short_name for c in calm_calls(entries)] == ["search"]
