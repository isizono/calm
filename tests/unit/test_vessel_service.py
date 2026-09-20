"""vessel_service（知見を作る・追記する・引く3つの処理）の単体テスト。

拒否コードの全種類が意図した条件で返ること、拒否が例外にならず通常の戻り値で
返ること、条件JSONの正規化、類似検査の閾値、get_lessonsの配達判定を検証する。
"""
import json

import pytest

from src.db import get_connection
from src.services import vessel_service as vs
from src.services.vessel_rules import canonical_spec


# ---------------------------------------------------------------------------
# obs_events への挿入ヘルパー（人間の裏づけを最小構成で作る）
# ---------------------------------------------------------------------------


def _human_bind(conn, lesson_id, session_id="s1", prompt_id="p1", entry_id=None, text="訂正の発話"):
    """1つの発話がそのままそのターンを構成し、そのターンでbindするという、
    人間の裏づけが成立する最小の実在しうる状態を作る（許可リストの組で発話する）。
    """
    cur = conn.execute(
        "INSERT INTO obs_events (session_id, kind, prompt_id, text) VALUES (?,?,?,?)",
        (session_id, "utterance", prompt_id, text),
    )
    uid = cur.lastrowid
    conn.execute(
        "INSERT INTO obs_events (session_id, kind, ref_id, text) VALUES (?,?,?,?)",
        (session_id, "speaker", uid, json.dumps({"turnOrigin": "human", "promptSource": "typed"})),
    )
    conn.execute(
        "INSERT INTO obs_events (session_id, kind, prompt_id, lesson_id, entry_id, ref_id) "
        "VALUES (?,?,?,?,?,?)",
        (session_id, "bind", prompt_id, lesson_id, entry_id, uid),
    )
    conn.commit()


def _prevent_kwargs(handle, body="ある操作は危険なので避ける", tool_value="foo", quote=None):
    return dict(
        kind="prevent", handle=handle, body=body,
        deliver_event="tool_call",
        deliver_spec={"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": tool_value}]},
        step_event="tool_call",
        step_spec={"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": tool_value}]},
        quote=quote,
    )


def _lesson_id(conn, handle):
    return conn.execute("SELECT id FROM lessons WHERE handle = ?", (handle,)).fetchone()["id"]


# ---------------------------------------------------------------------------
# 拒否コードの全種類
# ---------------------------------------------------------------------------


class TestRejectCodes:
    def test_vessel_off_blocks_record_lesson(self, temp_db):
        conn = get_connection()
        conn.execute("UPDATE vessel_meta SET mode = 'off' WHERE id = 1")
        conn.commit()
        conn.close()
        result = vs.record_lesson(kind="tally", handle="off-tally", body="判断の癖のメモ")
        assert result == {
            "ok": False,
            "error": {
                "code": "vessel_off",
                "message": "vessel_meta.mode が off",
                "fix": "vessel_meta.mode が 'on' か 'observe' になるまで待つ",
            },
        }

    def test_vessel_off_blocks_append_lesson(self, temp_db):
        vs.record_lesson(kind="tally", handle="a-tally", body="判断の癖のメモ")
        conn = get_connection()
        conn.execute("UPDATE vessel_meta SET mode = 'off' WHERE id = 1")
        conn.commit()
        conn.close()
        result = vs.append_lesson(handle="a-tally", kind="note", note="補足")
        assert result["error"]["code"] == "vessel_off"

    def test_observe_mode_allows_write(self, temp_db):
        # 既定のmodeはobserve（migrations/0075）。書けることを確かめる。
        result = vs.record_lesson(kind="tally", handle="observe-tally", body="判断の癖のメモ")
        assert result["ok"] is True

    def test_get_lessons_works_when_off(self, temp_db):
        vs.record_lesson(kind="tally", handle="readable-tally", body="判断の癖のメモ")
        conn = get_connection()
        conn.execute("UPDATE vessel_meta SET mode = 'off' WHERE id = 1")
        conn.commit()
        conn.close()
        result = vs.get_lessons(handle="readable-tally")
        assert result["ok"] is True
        assert result["items"][0]["handle"] == "readable-tally"

    def test_invalid_spec_bad_regex(self, temp_db):
        kwargs = _prevent_kwargs("bad-regex", tool_value="(")
        result = vs.record_lesson(**kwargs)
        assert result["error"]["code"] == "invalid_spec"

    def test_invalid_spec_bad_op(self, temp_db):
        result = vs.record_lesson(
            kind="prevent", handle="bad-op", body="ある操作は危険",
            deliver_event="tool_call",
            deliver_spec={"all": [{"field": "command", "op": "startswith", "value": "x"}]},
            step_event="tool_call",
            step_spec={"all": [{"field": "command", "op": "regex", "value": "x"}]},
        )
        assert result["error"]["code"] == "invalid_spec"

    def test_invalid_spec_too_many_clauses(self, temp_db):
        clauses = [{"field": "command", "op": "regex", "value": str(i)} for i in range(4)]
        result = vs.record_lesson(
            kind="prevent", handle="too-many-clauses", body="ある操作は危険",
            deliver_event="tool_call", deliver_spec={"all": clauses},
            step_event="tool_call", step_spec={"all": clauses},
        )
        assert result["error"]["code"] == "invalid_spec"

    def test_kind_mismatch_unknown_kind(self, temp_db):
        result = vs.record_lesson(kind="nonexistent", handle="unknown-kind", body="body")
        assert result["error"]["code"] == "kind_mismatch"

    def test_kind_mismatch_condition_on_tally(self, temp_db):
        result = vs.record_lesson(
            kind="tally", handle="tally-with-cond", body="body",
            deliver_event="tool_call", deliver_spec={},
        )
        assert result["error"]["code"] == "kind_mismatch"

    def test_seed_only_blocks_guide_creation(self, temp_db):
        result = vs.record_lesson(
            kind="guide", handle="seed-attempt", body="手順の候補",
            deliver_event="prompt", deliver_spec={},
        )
        assert result["error"]["code"] == "seed_only"

    def test_no_session_channel_on_seeded_guide_lesson(self, temp_db):
        # guideはrecord_lessonから作れないので、初期値投入と同じ形でSQLを直接使う。
        conn = get_connection()
        conn.execute(
            "INSERT INTO lessons (kind, handle, body, deliver_event, deliver_spec) "
            "VALUES ('guide', 'seeded-guide', 'body', 'prompt', '{}')"
        )
        conn.commit()
        conn.close()
        result = vs.append_lesson(
            handle="seeded-guide", kind="conditions",
            deliver_event="session", deliver_spec={},
        )
        assert result["error"]["code"] == "no_session_channel"

    def test_duplicate_same_handle(self, temp_db):
        kwargs = _prevent_kwargs("dup-handle")
        assert vs.record_lesson(**kwargs)["ok"] is True
        result = vs.record_lesson(**{**kwargs, "body": "別の本文にしても同じhandleは弾かれる"})
        assert result["error"]["code"] == "duplicate"

    def test_duplicate_same_conditions_different_handle(self, temp_db):
        assert vs.record_lesson(**_prevent_kwargs("cond-a", tool_value="same-regex"))["ok"] is True
        result = vs.record_lesson(**_prevent_kwargs("cond-b", body="別の本文", tool_value="same-regex"))
        assert result["error"]["code"] == "duplicate"

    def test_similar_exists_rejects_above_threshold(self, temp_db):
        base = "テストが落ちたときは必ずrootcauseを直す"
        near = "テストが落ちたときは必ずrootcauseを直すこと"
        assert vs.record_lesson(**_prevent_kwargs("similar-base", body=base, tool_value="a"))["ok"] is True
        result = vs.record_lesson(**_prevent_kwargs("similar-near", body=near, tool_value="b"))
        assert result["error"]["code"] == "similar_exists"

    def test_session_pool_full_rejects_overflow(self, temp_db):
        conn = get_connection()
        # 常時枠(1500字)は人間由来の知見だけを数えるので、既存の5件をbindで人間由来にする。
        for i in range(5):
            handle = f"pool-{i}"
            # 各知見の本文は別の文字の繰り返しにする（同じ本文だと類似検査に先に引っかかるため）。
            body = "abcdefghij"[i] * 300
            create = vs.record_lesson(
                kind="prevent", handle=handle, body=body,
                deliver_event="session", deliver_spec={},
                step_event="tool_call",
                step_spec={"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": f"v{i}"}]},
            )
            assert create["ok"] is True, create
            _human_bind(conn, _lesson_id(conn, handle), session_id=f"s{i}", prompt_id=f"p{i}")
        conn.close()
        result = vs.record_lesson(
            kind="prevent", handle="pool-overflow", body="y",
            deliver_event="session", deliver_spec={},
            step_event="tool_call",
            step_spec={"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": "vX"}]},
        )
        assert result["error"]["code"] == "session_pool_full"

    def test_session_pool_not_counted_for_ai_origin(self, temp_db):
        # bindが無い（AI由来のまま）既存知見は常時枠の合計に数えない。
        for i in range(5):
            body = "abcdefghij"[i] * 300
            create = vs.record_lesson(
                kind="prevent", handle=f"aipool-{i}", body=body,
                deliver_event="session", deliver_spec={},
                step_event="tool_call",
                step_spec={"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": f"w{i}"}]},
            )
            assert create["ok"] is True, create
        result = vs.record_lesson(
            kind="prevent", handle="aipool-new", body="y",
            deliver_event="session", deliver_spec={},
            step_event="tool_call",
            step_spec={"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": "wX"}]},
        )
        assert result["ok"] is True

    def test_protected_blocks_body_append(self, temp_db):
        conn = get_connection()
        handle = "protected-body"
        vs.record_lesson(**_prevent_kwargs(handle))
        _human_bind(conn, _lesson_id(conn, handle))
        conn.close()
        result = vs.append_lesson(handle=handle, kind="body", body="上書きしたい本文")
        assert result["error"]["code"] == "protected"

    def test_protected_blocks_conditions_and_withdraw(self, temp_db):
        conn = get_connection()
        handle = "protected-cw"
        vs.record_lesson(**_prevent_kwargs(handle))
        _human_bind(conn, _lesson_id(conn, handle))
        conn.close()
        cond_result = vs.append_lesson(
            handle=handle, kind="conditions",
            deliver_event="tool_call",
            deliver_spec={"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": "other"}]},
            step_event="tool_call",
            step_spec={"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": "other"}]},
        )
        assert cond_result["error"]["code"] == "protected"
        withdraw_result = vs.append_lesson(handle=handle, kind="withdraw")
        assert withdraw_result["error"]["code"] == "protected"

    def test_protected_lesson_still_accepts_note_and_violated(self, temp_db):
        conn = get_connection()
        handle = "protected-but-note"
        vs.record_lesson(**_prevent_kwargs(handle))
        _human_bind(conn, _lesson_id(conn, handle))
        conn.close()
        note_result = vs.append_lesson(handle=handle, kind="note", note="補足しておく")
        assert note_result["ok"] is True
        violated_result = vs.append_lesson(
            handle=handle, kind="violated", quote="これで十分な逐語の引用になる文字数"
        )
        assert violated_result["ok"] is True

    def test_unknown_lesson_missing_handle(self, temp_db):
        result = vs.append_lesson(handle="does-not-exist", kind="note", note="補足")
        assert result["error"]["code"] == "unknown_lesson"

    def test_unknown_lesson_retracted_handle(self, temp_db):
        handle = "will-retract"
        vs.record_lesson(kind="tally", handle=handle, body="判断の癖のメモ")
        assert vs.append_lesson(handle=handle, kind="withdraw")["ok"] is True
        result = vs.append_lesson(handle=handle, kind="note", note="補足")
        assert result["error"]["code"] == "unknown_lesson"


# ---------------------------------------------------------------------------
# 拒否は例外にせず通常の戻り値として返る
# ---------------------------------------------------------------------------


class TestRejectionsAreNormalReturns:
    def test_record_lesson_never_raises_on_bad_input(self, temp_db):
        # 意図的に壊れた入力を連投しても例外が外へ漏れないことを確かめる。
        results = [
            vs.record_lesson(kind="prevent", handle="x", body="b",
                              deliver_event="tool_call", deliver_spec={"all": "not-a-list"}),
            vs.record_lesson(kind=None, handle="y", body="b"),
            vs.record_lesson(kind="prevent", handle="z", body="b",
                              deliver_event="tool_call",
                              deliver_spec={"all": [{"field": "command", "op": "regex", "value": 123}]}),
        ]
        for r in results:
            assert isinstance(r, dict)
            assert r["ok"] is False
            assert "code" in r["error"]

    def test_append_lesson_never_raises_on_bad_kind(self, temp_db):
        result = vs.append_lesson(handle="whatever", kind="not-a-real-kind")
        assert isinstance(result, dict)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# 条件JSONの正規化
# ---------------------------------------------------------------------------


class TestCanonicalSpec:
    def test_key_order_and_whitespace_are_absorbed(self):
        a = {"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": r"\bp(grep|kill)\s+-f\b"}]}
        b = {"all": [{"op": "regex", "field": " command ", "value": r"\bp(grep|kill)\s+-f\b "}], "tool": "Bash"}
        assert canonical_spec(a) == canonical_spec(b)

    def test_internal_whitespace_in_regex_is_not_collapsed(self):
        a = {"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": r"\bp(grep|kill)\s+-f\b"}]}
        c = {"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": r"\bp(grep|kill)\s+ -f\b"}]}
        assert canonical_spec(a) != canonical_spec(c)

    def test_clause_order_is_preserved_not_sorted(self):
        a = {"all": [{"field": "a", "op": "regex", "value": "1"}, {"field": "b", "op": "regex", "value": "2"}]}
        b = {"all": [{"field": "b", "op": "regex", "value": "2"}, {"field": "a", "op": "regex", "value": "1"}]}
        assert canonical_spec(a) != canonical_spec(b)


# ---------------------------------------------------------------------------
# 類似検査の閾値
# ---------------------------------------------------------------------------


class TestSimilarityThreshold:
    def test_below_warn_threshold_is_silent(self, temp_db):
        vs.record_lesson(**_prevent_kwargs("baseline-a", body="全く違う内容の知見その1", tool_value="p"))
        result = vs.record_lesson(**_prevent_kwargs("baseline-b", body="別方向の話題その2です", tool_value="q"))
        assert result["ok"] is True
        assert not any("近い知見がある" in w for w in result["warnings"])

    def test_between_thresholds_warns_but_succeeds(self, temp_db):
        vs.record_lesson(**_prevent_kwargs(
            "warn-a", body="コマンド実行前に必ず対象ディレクトリを確認する", tool_value="p"))
        result = vs.record_lesson(**_prevent_kwargs(
            "warn-b", body="コマンド実行前に確認する習慣を持つ", tool_value="q"))
        assert result["ok"] is True
        assert any("近い知見がある" in w for w in result["warnings"])

    def test_declaring_not_same_as_bypasses_rejection(self, temp_db):
        base = "テストが落ちたときは必ずrootcauseを直す"
        near = "テストが落ちたときは必ずrootcauseを直すこと"
        vs.record_lesson(**_prevent_kwargs("declare-a", body=base, tool_value="p"))
        result = vs.record_lesson(
            **_prevent_kwargs("declare-b", body=near, tool_value="q"), not_same_as=["declare-a"]
        )
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# get_lessons: delivered_handles
# ---------------------------------------------------------------------------


class TestGetLessonsDeliveredHandles:
    def test_delivered_handles_excludes_non_delivering_kind(self, temp_db):
        vs.record_lesson(**_prevent_kwargs("deliver-me"))
        vs.record_lesson(kind="tally", handle="tally-only", body="配達しない判断の癖")
        result = vs.get_lessons()
        handles = {d["handle"] for d in result["delivered_handles"]}
        assert "deliver-me" in handles
        assert "tally-only" not in handles

    def test_delivered_handles_include_body(self, temp_db):
        vs.record_lesson(**_prevent_kwargs("deliver-with-body", body="踏むと危険な操作を避ける"))
        result = vs.get_lessons()
        entry = next(d for d in result["delivered_handles"] if d["handle"] == "deliver-with-body")
        assert entry["body"] == "踏むと危険な操作を避ける"

    def test_delivered_handles_excludes_retracted(self, temp_db):
        handle = "deliver-then-retract"
        vs.record_lesson(**_prevent_kwargs(handle))
        vs.append_lesson(handle=handle, kind="withdraw")
        result = vs.get_lessons(handle=handle)
        assert result["items"] == []
        assert handle not in {d["handle"] for d in result["delivered_handles"]}
