"""scripts/recorder_harness.py のcheckの単体テスト。

入力は記録役のtranscriptに実際に書かれる形の行（起動プロンプト・見張りの
asyncRewakeによる片の指示・tool_use・tool_result・最終テキスト）で組み立てる。
"""
import json

from scripts.recorder_harness import check, read_entries

_BOOT = "起動確認。片が届くまで何もせず `READY` とだけ返してターンを終えよ"


def _prompt(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


def _dispatch(no: int, prefix: str = "") -> dict:
    return _prompt(
        "<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n"
        f"Stop hook blocking error from command \"Stop\": {prefix}片{no:04d}を処理せよ。Read /x/{no:04d}.md。"
        f"終えたら最終行に `DONE {no:04d}` と書いてターンを終えよ"
    )


def _say(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _call(use_id: str, tool: str, result: dict | None = None, *, denied: bool = False) -> list[dict]:
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": use_id, "name": f"mcp__calm__{tool}", "input": {}},
    ]}}]
    block = {"type": "tool_result", "tool_use_id": use_id,
             "content": "片を受け取る前はツールを使えない" if denied else json.dumps(result or {})}
    if denied:
        block["is_error"] = True
    lines.append({"type": "user", "message": {"content": [block]}})
    return lines


def _run(tmp_path, lines: list[dict]) -> dict:
    path = tmp_path / "rec.jsonl"
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")
    return check(read_entries([path]))


_HIT = {"results": [{"type": "log", "id_raw": 4242}]}


def _good_shards() -> list[dict]:
    return [
        _dispatch(1),
        *_call("s1", "search", _HIT),
        *_call("w1", "add_logs", {"created": [{"log_id": 1}]}),
        _say("DONE 0001"),
        _dispatch(2),
        *_call("s2", "search", _HIT),
        _say("SKIP 重複 log:4242 同じ経緯\nDONE 0002"),
    ]


def test_well_behaved_run_passes(tmp_path):
    result = _run(tmp_path, [_prompt(_BOOT), _say("READY"), *_good_shards()])
    assert result["ok"] is True, result["failures"]
    assert result["off_shard_attempts"] == 0
    assert [(s["no"], s["searches"], s["writes"]) for s in result["shards"]] == [
        (1, 1, ["add_logs"]),
        (2, 1, []),
    ]


def test_calm_call_executed_before_first_shard_fails(tmp_path):
    lines = [_prompt(_BOOT), *_call("x", "search", _HIT), _say("READY"), *_good_shards()]
    result = _run(tmp_path, lines)
    assert result["ok"] is False
    assert len(result["failures"]) == 1
    assert "CALMツールが実行された: ['search']" in result["failures"][0]


def test_denied_call_before_first_shard_is_counted_but_passes(tmp_path):
    lines = [_prompt(_BOOT), *_call("x", "add_logs", denied=True), _say("READY"), *_good_shards()]
    result = _run(tmp_path, lines)
    assert result["ok"] is True, result["failures"]
    assert result["off_shard_attempts"] == 1


def test_calm_call_in_noop_turn_fails(tmp_path):
    lines = [_prompt(_BOOT), _say("READY"), *_good_shards(),
             _prompt("片なし。`DONE -` とだけ返せ"), *_call("n", "add_logs", {"created": []}), _say("DONE -")]
    result = _run(tmp_path, lines)
    assert result["ok"] is False
    assert "CALMツールが実行された: ['add_logs']" in result["failures"][0]


def test_done_number_mismatch_fails(tmp_path):
    lines = [_prompt(_BOOT), _say("READY"), _dispatch(3), *_call("s", "search", _HIT),
             _say("SKIP 該当なし 雑談\nDONE 0002")]
    result = _run(tmp_path, lines)
    assert result["failures"] == ["片0003: 最終メッセージのDONE番号が片番号と一致しない: [2]"]


def test_shard_without_search_fails(tmp_path):
    lines = [_prompt(_BOOT), _say("READY"), _dispatch(1),
             *_call("w", "add_material", {"material_id": 3}), _say("DONE 0001")]
    result = _run(tmp_path, lines)
    assert result["failures"] == ["片0001: 記録証跡が足りない: ['成功したsearchが無い']"]


def test_resent_shard_is_judged_across_both_turns(tmp_path):
    """証跡不足で再送された片は、1回目のsearchと再送後のSKIP行を合わせて判定する。"""
    lines = [
        _prompt(_BOOT), _say("READY"),
        _dispatch(1), *_call("s", "search", _HIT), _say("DONE 0001"),
        _dispatch(1, prefix="片0001のDONEは受け付けていない（不足: 記録もSKIP行も無い）。\n"),
        _say("SKIP 該当なし 雑談だけ\nDONE 0001"),
    ]
    result = _run(tmp_path, lines)
    assert result["ok"] is True, result["failures"]
    assert result["shards"][0]["turns"] == 2


def test_no_shard_at_all_fails(tmp_path):
    result = _run(tmp_path, [_prompt(_BOOT), _say("READY")])
    assert result["failures"] == ["片の指示を受けたターンが1つも無い"]
