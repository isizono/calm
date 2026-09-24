"""hooks/feedback_hook.py の単体テスト。

DBは実SQLite（temp_db、全migration適用）を使う。エントリの用意は
feedback_serviceの実関数（write_feedback_entry）で行い、hook自体は
main()にstdin JSONを流し込みstdoutのJSONを検証する（test_preblock_hook.pyと
同じ、sys.stdin monkeypatch + capsysのin-processパターン）。
"""
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import feedback_hook as hook  # type: ignore  # noqa: E402

from src.db import get_connection
from src.services import feedback_service as fs


@pytest.fixture
def db(monkeypatch, temp_db):
    """temp_dbはDISCUSSION_DB_PATHを設定するので、hookが読むCALM_DB_PATHへも
    明示的に向ける（env_compatのフォールバック順に頼らない、既存hookテストと同じ手法）。"""
    monkeypatch.setenv("CALM_DB_PATH", temp_db)
    return temp_db


def _run_main_with_event(event: dict, capsys) -> dict:
    """stdin に event を流して main() を呼び、stdout 出力を dict として返す。"""
    sys.stdin = io.StringIO(json.dumps(event))
    try:
        hook.main()
    finally:
        sys.stdin = sys.__stdin__
    captured = capsys.readouterr()
    text = captured.out.strip()
    if not text or text == "{}":
        return {}
    return json.loads(text)


def _create_entry(name: str, **overrides) -> dict:
    fields = {
        "name": name,
        "action": "create",
        "body": "テスト知見",
        "strength": "notify",
        "timing": "utterance",
        "condition": {"tool": None, "all": []},
    }
    fields.update(overrides)
    result = fs.write_feedback_entry(**fields)
    assert result["ok"], result
    return result


def _row(name: str):
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM feedback_entries WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()


def _shown_lines(body: str) -> list[str]:
    return [line for line in body.splitlines() if line.startswith("- ")]


# ---------------------------------------------------------------------------
# 純粋関数
# ---------------------------------------------------------------------------


class TestSelectShown:
    def test_respects_max_shown(self):
        candidates = [{"id": i} for i in range(5)]
        shown = hook._select_shown(candidates, budget_chars=1000, render=lambda c: "x")
        assert len(shown) == hook.MAX_SHOWN

    def test_respects_char_budget(self):
        candidates = [{"id": 1}]
        shown = hook._select_shown(candidates, budget_chars=5, render=lambda c: "x" * 6)
        assert shown == []

    def test_stops_once_budget_exceeded_even_if_a_later_entry_would_fit(self):
        candidates = [{"id": 1}, {"id": 2}]
        render_map = {1: "x" * 10, 2: "y"}
        shown = hook._select_shown(candidates, budget_chars=5, render=lambda c: render_map[c["id"]])
        assert shown == []

    def test_entries_within_budget_are_all_included(self):
        candidates = [{"id": 1}, {"id": 2}]
        shown = hook._select_shown(candidates, budget_chars=100, render=lambda c: "xx")
        assert len(shown) == 2


class TestExtractErrorText:
    def test_tool_error_key(self):
        assert hook._extract_error_text({"tool_error": "boom"}) == "boom"

    def test_error_key(self):
        assert hook._extract_error_text({"error": "boom"}) == "boom"

    def test_tool_response_dict_with_error(self):
        assert hook._extract_error_text({"tool_response": {"error": "boom"}}) == "boom"

    def test_tool_response_dict_with_message(self):
        assert hook._extract_error_text({"tool_response": {"message": "boom"}}) == "boom"

    def test_tool_response_dict_fallback_is_json(self):
        result = hook._extract_error_text({"tool_response": {"success": False}})
        assert "success" in result

    def test_tool_response_string(self):
        assert hook._extract_error_text({"tool_response": "boom"}) == "boom"

    def test_nothing_present_returns_empty_string(self):
        assert hook._extract_error_text({}) == ""


class TestFingerprint:
    def test_key_order_does_not_affect_fingerprint(self):
        assert hook._fingerprint({"a": 1, "b": 2}) == hook._fingerprint({"b": 2, "a": 1})

    def test_different_value_changes_fingerprint(self):
        assert hook._fingerprint({"a": 1}) != hook._fingerprint({"a": 2})


class TestModeOn:
    """_mode_on: DB接続失敗以外の『不正値』系(テーブル未作成・行欠落・mode='off')を

    生sqlite3接続で直接再現する（CHECK制約により'observe'等の不正値は書き込み
    自体ができないため、値の不正の代表としてoffと行欠落を検証する）。
    """

    def test_table_missing_is_off(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "no-table.db"))
        conn.row_factory = sqlite3.Row
        assert hook._mode_on(conn) is False
        conn.close()

    def test_row_missing_is_off(self, db):
        conn = get_connection()
        try:
            conn.execute("DELETE FROM feedback_switch")
            conn.commit()
        finally:
            conn.close()
        conn = get_connection()
        try:
            assert hook._mode_on(conn) is False
        finally:
            conn.close()

    def test_mode_off_is_off(self, db):
        conn = get_connection()
        try:
            conn.execute("UPDATE feedback_switch SET mode = 'off'")
            conn.commit()
        finally:
            conn.close()
        conn = get_connection()
        try:
            assert hook._mode_on(conn) is False
        finally:
            conn.close()

    def test_mode_on_is_on(self, db):
        conn = get_connection()
        try:
            assert hook._mode_on(conn) is True
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# main() 経由の結合テスト
# ---------------------------------------------------------------------------


class TestMainDispatch:
    def test_malformed_stdin_json_emits_empty(self, capsys):
        sys.stdin = io.StringIO("{not valid json")
        try:
            hook.main()
        finally:
            sys.stdin = sys.__stdin__
        assert capsys.readouterr().out.strip() == "{}"

    def test_unknown_event_name_emits_empty(self, db, capsys):
        out = _run_main_with_event({"hook_event_name": "SomeOtherEvent", "session_id": "s1"}, capsys)
        assert out == {}


class TestFailOpen:
    """DB接続不可・テーブル未作成はすべてfail-open（何も出さず終了）。"""

    def test_missing_tables_fails_open_for_all_three_events(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("CALM_DB_PATH", str(tmp_path / "empty-no-migrations.db"))
        cases = [
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "help"},
            {
                "hook_event_name": "PostToolUseFailure",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {},
                "error": "boom",
            },
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
            },
        ]
        for event in cases:
            assert _run_main_with_event(event, capsys) == {}, event["hook_event_name"]


class TestUserPromptSubmit:
    def test_no_session_id_emits_empty(self, db, capsys):
        out = _run_main_with_event({"hook_event_name": "UserPromptSubmit", "prompt": "help"}, capsys)
        assert out == {}

    def test_mode_off_emits_empty(self, db, capsys):
        _create_entry(
            "stump-a",
            condition={"tool": None, "all": [{"field": "prompt", "op": "len_gt", "value": 0}]},
        )
        conn = get_connection()
        try:
            conn.execute("UPDATE feedback_switch SET mode = 'off'")
            conn.commit()
        finally:
            conn.close()
        out = _run_main_with_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "hello"}, capsys
        )
        assert out == {}

    def test_no_matching_entry_emits_empty(self, db, capsys):
        out = _run_main_with_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "hello"}, capsys
        )
        assert out == {}

    def test_matching_entry_delivered_and_counted(self, db, capsys):
        _create_entry(
            "stump-a",
            body="躓きメモ",
            condition={"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "help"}]},
        )
        out = _run_main_with_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "please help",
            },
            capsys,
        )
        spec = out["hookSpecificOutput"]
        assert spec["hookEventName"] == "UserPromptSubmit"
        assert "躓きメモ" in spec["additionalContext"]
        assert "1回目" in spec["additionalContext"]
        assert _row("stump-a")["delivered_count"] == 1

    def test_second_call_same_prompt_id_is_suppressed(self, db, capsys):
        _create_entry(
            "stump-a",
            condition={"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "help"}]},
        )
        event = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s1",
            "prompt_id": "p1",
            "prompt": "please help",
        }
        _run_main_with_event(event, capsys)
        out = _run_main_with_event(event, capsys)
        assert out == {}
        assert _row("stump-a")["delivered_count"] == 1

    def test_different_prompt_id_delivers_again(self, db, capsys):
        _create_entry(
            "stump-a",
            condition={"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "help"}]},
        )
        _run_main_with_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt_id": "p1", "prompt": "help"},
            capsys,
        )
        out = _run_main_with_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt_id": "p2", "prompt": "help"},
            capsys,
        )
        assert out != {}
        assert _row("stump-a")["delivered_count"] == 2

    def test_at_most_3_entries_shown(self, db, capsys):
        for i in range(5):
            _create_entry(
                f"stump-{i}",
                condition={"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "help"}]},
            )
        out = _run_main_with_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "help"}, capsys
        )
        body = out["hookSpecificOutput"]["additionalContext"]
        assert len(_shown_lines(body)) == 3

    def test_entries_dropped_by_cap_are_not_counted_as_delivered(self, db, capsys):
        """#8: 件数上限で落ちた分はdelivered_countを増やさない。"""
        for i in range(5):
            _create_entry(
                f"stump-{i}",
                condition={"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "help"}]},
            )
        _run_main_with_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "help"}, capsys
        )
        counts = [_row(f"stump-{i}")["delivered_count"] for i in range(5)]
        assert sorted(counts) == [0, 0, 1, 1, 1]

    def test_deleted_entry_is_not_matched(self, db, capsys):
        """#4: deleted_atの付いたエントリは配達にも照合にも使われない。"""
        _create_entry(
            "stump-a",
            condition={"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "help"}]},
        )
        del_result = fs.write_feedback_entry(name="stump-a", action="delete", read_mark=0)
        assert del_result["ok"], del_result
        out = _run_main_with_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "please help"}, capsys
        )
        assert out == {}

    def test_broken_regex_entry_is_skipped_others_still_evaluated(self, db, capsys):
        """#7: 保存済みエントリの正規表現評価で例外が出たら、そのエントリだけ
        スキップし、他のエントリの評価は継続する。write時の検証をすり抜けた
        想定として、正規表現の妥当性チェックを経由しない生SQLで壊れた値を仕込む。"""
        _create_entry(
            "broken",
            condition={"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "help"}]},
        )
        _create_entry(
            "healthy",
            body="正常な知見",
            condition={"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "help"}]},
        )
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE feedback_entries SET condition_json = ? WHERE name = 'broken'",
                (json.dumps({"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "("}]}),),
            )
            conn.commit()
        finally:
            conn.close()
        out = _run_main_with_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "please help"}, capsys
        )
        assert "正常な知見" in out["hookSpecificOutput"]["additionalContext"]
        assert _row("broken")["delivered_count"] == 0
        assert _row("healthy")["delivered_count"] == 1


class TestPostToolUseFailure:
    def test_no_session_id_emits_empty(self, db, capsys):
        out = _run_main_with_event({"hook_event_name": "PostToolUseFailure"}, capsys)
        assert out == {}

    def test_no_matching_entry_triggers_bootstrap_once_per_session(self, db, capsys):
        event = {
            "hook_event_name": "PostToolUseFailure",
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {},
            "error": "boom",
        }
        out1 = _run_main_with_event(event, capsys)
        assert "write_feedback_entry" in out1["hookSpecificOutput"]["additionalContext"]
        out2 = _run_main_with_event(event, capsys)
        assert out2 == {}

    def test_matching_entry_is_delivered(self, db, capsys):
        _create_entry(
            "fail-a",
            body="タイムアウトの躓き",
            timing="tool_fail",
            condition={"tool": None, "all": [{"field": "error", "op": "regex", "value": "timeout"}]},
        )
        out = _run_main_with_event(
            {
                "hook_event_name": "PostToolUseFailure",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {},
                "error": "connection timeout",
            },
            capsys,
        )
        assert "タイムアウトの躓き" in out["hookSpecificOutput"]["additionalContext"]
        assert _row("fail-a")["delivered_count"] == 1

    def test_matched_but_all_suppressed_by_turn_mark_emits_empty_without_bootstrap(self, db, capsys):
        _create_entry(
            "fail-a",
            timing="tool_fail",
            condition={"tool": None, "all": [{"field": "error", "op": "regex", "value": "timeout"}]},
        )
        event = {
            "hook_event_name": "PostToolUseFailure",
            "session_id": "s1",
            "prompt_id": "p1",
            "tool_name": "Bash",
            "tool_input": {},
            "error": "connection timeout",
        }
        _run_main_with_event(event, capsys)  # 1回目: 配達・turn_mark登録
        out = _run_main_with_event(event, capsys)  # 2回目: shownは空(matchedは非0)
        assert out == {}
        conn = get_connection()
        try:
            seen = conn.execute(
                "SELECT 1 FROM feedback_bootstrap_seen WHERE session_id = ?", ("s1",)
            ).fetchone()
        finally:
            conn.close()
        assert seen is None  # matched非0のときbootstrapは出さない


class TestPreToolUse:
    def test_no_matching_block_entry_emits_empty(self, db, capsys):
        out = _run_main_with_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            },
            capsys,
        )
        assert out == {}

    def test_first_hit_denies_and_registers_hold(self, db, capsys):
        _create_entry(
            "danger", strength="block", timing="pre_tool", condition={"tool": "Bash", "all": []}
        )
        out = _run_main_with_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
            },
            capsys,
        )
        spec = out["hookSpecificOutput"]
        assert spec["permissionDecision"] == "deny"
        assert _row("danger")["delivered_count"] == 1
        conn = get_connection()
        try:
            hold = conn.execute(
                "SELECT fingerprint FROM feedback_holds WHERE session_id = ?", ("s1",)
            ).fetchone()
        finally:
            conn.close()
        assert hold is not None

    def test_same_args_second_call_pushes_through(self, db, capsys):
        """1回止めの往復(#1/#2/#3の中核): 同じ引数の再実行で押し切られる。"""
        _create_entry(
            "danger", strength="block", timing="pre_tool", condition={"tool": "Bash", "all": []}
        )
        event = {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        }
        _run_main_with_event(event, capsys)
        out = _run_main_with_event(event, capsys)
        assert out == {}
        row = _row("danger")
        assert row["overridden_count"] == 1
        assert row["delivered_count"] == 1  # 押し切り時はdelivered_countを増やさない
        conn = get_connection()
        try:
            hold = conn.execute(
                "SELECT 1 FROM feedback_holds WHERE session_id = ?", ("s1",)
            ).fetchone()
        finally:
            conn.close()
        assert hold is None

    def test_different_args_blocks_again_with_replaced_fingerprint(self, db, capsys):
        """引数が1文字でも異なれば再度ブロックする(#1)。"""
        _create_entry(
            "danger", strength="block", timing="pre_tool", condition={"tool": "Bash", "all": []}
        )
        first = {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /a"},
        }
        second = {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /b"},
        }
        _run_main_with_event(first, capsys)
        out = _run_main_with_event(second, capsys)
        spec = out["hookSpecificOutput"]
        assert spec["permissionDecision"] == "deny"
        row = _row("danger")
        assert row["delivered_count"] == 2
        assert row["overridden_count"] == 0
        # 保留は直近(second)の指紋に置き換わっているため、firstで再度呼んでも
        # まだ押し切れない(複数指紋の同時保持はしない)
        third = _run_main_with_event(first, capsys)
        assert third["hookSpecificOutput"]["permissionDecision"] == "deny"
        # 保留はfirstの指紋に置き換わった。直近と同じfirstを再送すれば今度は押し切れる
        fourth = _run_main_with_event(first, capsys)
        assert fourth == {}
        assert _row("danger")["overridden_count"] == 1

    def test_multiple_blocking_entries_combined_into_a_single_deny(self, db, capsys):
        """#3: 複数のblockエントリに同時に当たったら、全部の理由をまとめて1回で止める。"""
        _create_entry(
            "danger-a",
            strength="block",
            timing="pre_tool",
            body="危険A",
            condition={"tool": "Bash", "all": []},
        )
        _create_entry(
            "danger-b",
            strength="block",
            timing="pre_tool",
            body="危険B",
            condition={"tool": "Bash", "all": []},
        )
        out = _run_main_with_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
            },
            capsys,
        )
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "危険A" in reason
        assert "危険B" in reason
        assert _row("danger-a")["delivered_count"] == 1
        assert _row("danger-b")["delivered_count"] == 1

    def test_all_blocking_entries_pushed_through_simultaneously_emits_empty(self, db, capsys):
        """#3: 再実行のときは、当たった全エントリの押し切られた回数を1ずつ増やす。"""
        _create_entry(
            "danger-a", strength="block", timing="pre_tool", condition={"tool": "Bash", "all": []}
        )
        _create_entry(
            "danger-b", strength="block", timing="pre_tool", condition={"tool": "Bash", "all": []}
        )
        event = {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        }
        _run_main_with_event(event, capsys)
        out = _run_main_with_event(event, capsys)
        assert out == {}
        assert _row("danger-a")["overridden_count"] == 1
        assert _row("danger-b")["overridden_count"] == 1

    def test_subagent_shares_parent_session_id_can_push_through(self, db, capsys):
        """#2: サブエージェントは親と同じsession_idとして扱われる。同じsession_idで
        同じ引数の呼び出しが来れば(呼び出し元がサブエージェントであっても)通す。"""
        _create_entry(
            "danger", strength="block", timing="pre_tool", condition={"tool": "Bash", "all": []}
        )
        event = {
            "hook_event_name": "PreToolUse",
            "session_id": "shared-session",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        }
        _run_main_with_event(event, capsys)  # 親での初回ブロック
        out = _run_main_with_event(event, capsys)  # 同一session_idでの再実行
        assert out == {}
        assert _row("danger")["overridden_count"] == 1

    def test_deleted_block_entry_does_not_block(self, db, capsys):
        """#4: deleted_atの付いたエントリは配達にも照合にも使われない。"""
        _create_entry(
            "danger", strength="block", timing="pre_tool", condition={"tool": "Bash", "all": []}
        )
        del_result = fs.write_feedback_entry(name="danger", action="delete", read_mark=0)
        assert del_result["ok"], del_result
        out = _run_main_with_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
            },
            capsys,
        )
        assert out == {}

    def test_over_3_blocking_entries_all_denied_but_reason_capped_at_3(self, db, capsys):
        """#16: 判定(deny)は当たった全件を反映し、理由文への表示は3件までに絞る。
        表示から落ちたブロック対象もholdへの登録・denyの判定自体には反映される。"""
        for i in range(4):
            _create_entry(
                f"danger-{i}",
                strength="block",
                timing="pre_tool",
                body=f"危険{i}",
                condition={"tool": "Bash", "all": []},
            )
        event = {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        }
        out = _run_main_with_event(event, capsys)
        spec = out["hookSpecificOutput"]
        assert spec["permissionDecision"] == "deny"
        reason = spec["permissionDecisionReason"]
        assert len(_shown_lines(reason)) == 3
        # 4件目は理由文には出ないが、holdは登録されdelivered_countは増えない
        assert _row("danger-3")["delivered_count"] == 0
        conn = get_connection()
        try:
            hold = conn.execute(
                "SELECT 1 FROM feedback_holds WHERE session_id = ? AND entry_id = ?",
                ("s1", _row("danger-3")["id"]),
            ).fetchone()
        finally:
            conn.close()
        assert hold is not None
        # 4件目も、同じ引数で再実行すれば(holdが効いて)押し切られる
        out2 = _run_main_with_event(event, capsys)
        assert out2 == {}
        assert _row("danger-3")["overridden_count"] == 1


class TestMaintenanceHintReviewLine:
    def test_review_line_appears_only_on_10th_delivery(self, db, capsys):
        _create_entry("stump-a", body="躓きメモ", condition={"tool": None, "all": []})
        for i in range(1, 10):
            out = _run_main_with_event(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "s1",
                    "prompt_id": f"p{i}",
                    "prompt": "hello",
                },
                capsys,
            )
            body = out["hookSpecificOutput"]["additionalContext"]
            assert "見直し時期" not in body, f"{i}回目で出てはいけない"
        out = _run_main_with_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p10",
                "prompt": "hello",
            },
            capsys,
        )
        body = out["hookSpecificOutput"]["additionalContext"]
        assert "見直し時期" in body

    def test_delivered_count_marker_appears_only_on_entry_line(self, db, capsys):
        _create_entry("stump-a", body="躓きメモ", condition={"tool": None, "all": []})
        for i in range(1, 11):
            out = _run_main_with_event(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "s1",
                    "prompt_id": f"p{i}",
                    "prompt": "hello",
                },
                capsys,
            )
        body = out["hookSpecificOutput"]["additionalContext"]
        lines = body.splitlines()
        entry_lines = [line for line in lines if line.startswith("- ")]
        other_lines = [line for line in lines if not line.startswith("- ")]
        assert "(10回目)" in entry_lines[0]
        for line in other_lines:
            assert "回目" not in line


class TestMaintenanceHintPromoteLine:
    def test_promote_line_appears_after_3_stumbles_and_clears_after_note(self, db, capsys):
        _create_entry("stump-a", body="躓きメモ", condition={"tool": None, "all": []})
        for _ in range(3):
            note = fs.add_feedback_note(name="stump-a", kind="stumble", body="踏んだ")
            assert note["ok"], note
        out = _run_main_with_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt_id": "p1", "prompt": "hello"},
            capsys,
        )
        assert "未処理の躓き3件" in out["hookSpecificOutput"]["additionalContext"]

        note = fs.add_feedback_note(name="stump-a", kind="note", body="対応した")
        assert note["ok"], note
        out = _run_main_with_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt_id": "p2", "prompt": "hello"},
            capsys,
        )
        assert "未処理の躓き" not in out["hookSpecificOutput"]["additionalContext"]

    def test_promote_line_does_not_reappear_with_only_1_stumble_after_note(self, db, capsys):
        _create_entry("stump-a", body="躓きメモ", condition={"tool": None, "all": []})
        for _ in range(3):
            fs.add_feedback_note(name="stump-a", kind="stumble", body="踏んだ")
        fs.add_feedback_note(name="stump-a", kind="note", body="対応した")
        fs.add_feedback_note(name="stump-a", kind="stumble", body="また踏んだ")
        out = _run_main_with_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt_id": "p1", "prompt": "hello"},
            capsys,
        )
        assert "未処理の躓き" not in out["hookSpecificOutput"]["additionalContext"]

    def test_pending_stumbles_do_not_mix_across_entries(self, db, capsys):
        """複数エントリがDBに同居し、それぞれstumble件数が異なるとき、格上げ行の
        件数は各エントリ自身のpending_stumblesだけを反映する(他エントリの分が
        混ざらない)。PENDING_STUMBLES_SQLの相関条件(n.entry_id = e.id)が正しく
        効いていることの回帰検知。"""
        _create_entry("entry-a", body="Aの躓き", condition={"tool": None, "all": []})
        _create_entry("entry-b", body="Bの躓き", condition={"tool": None, "all": []})
        for _ in range(3):
            note = fs.add_feedback_note(name="entry-a", kind="stumble", body="踏んだ")
            assert note["ok"], note
        note = fs.add_feedback_note(name="entry-b", kind="stumble", body="踏んだ")
        assert note["ok"], note

        out = _run_main_with_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt_id": "p1", "prompt": "hello"},
            capsys,
        )
        body = out["hookSpecificOutput"]["additionalContext"]
        # entry-a(3件)にだけ格上げ行が出て、entry-b(1件、閾値未満)には出ない。
        # 相関がずれて両者のstumbleが合算されると"4件"や2箇所出現になる。
        assert body.count("未処理の躓き") == 1
        assert "未処理の躓き3件" in body

    def test_deny_reason_includes_promote_line(self, db, capsys):
        _create_entry(
            "danger", strength="block", timing="pre_tool", body="危険",
            condition={"tool": "Bash", "all": []},
        )
        for _ in range(3):
            note = fs.add_feedback_note(name="danger", kind="stumble", body="踏んだ")
            assert note["ok"], note
        out = _run_main_with_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
            },
            capsys,
        )
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "未処理の躓き3件" in reason


class TestAgentTypeSuppressesUtteranceOnly:
    """サブエージェント発のUserPromptSubmit(agent_typeがtruthy)は発話タイミングの
    配達を止めるが、ツール失敗・実行直前の配達は続ける。"""

    def test_agent_type_suppresses_utterance_delivery(self, db, capsys):
        _create_entry(
            "stump-a",
            condition={"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "help"}]},
        )
        out = _run_main_with_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "please help",
                "agent_type": "general-purpose",
            },
            capsys,
        )
        assert out == {}
        assert _row("stump-a")["delivered_count"] == 0

    def test_agent_type_does_not_suppress_tool_fail_delivery(self, db, capsys):
        _create_entry(
            "fail-a",
            body="タイムアウトの躓き",
            timing="tool_fail",
            condition={"tool": None, "all": [{"field": "error", "op": "regex", "value": "timeout"}]},
        )
        out = _run_main_with_event(
            {
                "hook_event_name": "PostToolUseFailure",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {},
                "error": "connection timeout",
                "agent_type": "general-purpose",
            },
            capsys,
        )
        assert "タイムアウトの躓き" in out["hookSpecificOutput"]["additionalContext"]

    def test_agent_type_does_not_suppress_pre_tool_block(self, db, capsys):
        _create_entry(
            "danger", strength="block", timing="pre_tool", condition={"tool": "Bash", "all": []}
        )
        out = _run_main_with_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
                "agent_type": "general-purpose",
            },
            capsys,
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
