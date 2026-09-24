"""feedback_service（get_feedback_entries・write_feedback_entry・add_feedback_note）の単体テスト。"""
from src.db import get_connection
from src.services import feedback_service as fs


def _create(name="stump-1", **overrides) -> dict:
    fields = {
        "name": name,
        "action": "create",
        "body": "テスト本文",
        "strength": "notify",
        "timing": "utterance",
        "condition": {"tool": None, "all": [{"field": "prompt", "op": "len_gt", "value": 0}]},
    }
    fields.update(overrides)
    return fs.write_feedback_entry(**fields)


class TestGetFeedbackEntries:
    def test_empty_when_no_entries(self, temp_db):
        result = fs.get_feedback_entries()
        assert result == {"ok": True, "entries": []}

    def test_returns_created_entry_with_empty_notes_and_zero_read_mark(self, temp_db):
        _create()
        result = fs.get_feedback_entries()
        assert len(result["entries"]) == 1
        entry = result["entries"][0]
        assert entry["name"] == "stump-1"
        assert entry["notes"] == []
        assert entry["read_mark"] == 0
        assert entry["delivered_count"] == 0
        assert entry["overridden_count"] == 0
        assert entry["deleted_at"] is None

    def test_name_filter_exact_match(self, temp_db):
        _create("entry-a")
        _create("entry-b")
        result = fs.get_feedback_entries(name="entry-a")
        assert [e["name"] for e in result["entries"]] == ["entry-a"]

    def test_query_filter_matches_body(self, temp_db):
        _create("entry-a", body="キャッシュの罠")
        _create("entry-b", body="無関係な内容")
        result = fs.get_feedback_entries(query="キャッシュ")
        assert [e["name"] for e in result["entries"]] == ["entry-a"]

    def test_query_filter_matches_ref(self, temp_db):
        _create("entry-a", ref="https://example.com/incident-123")
        _create("entry-b")
        result = fs.get_feedback_entries(query="incident-123")
        assert [e["name"] for e in result["entries"]] == ["entry-a"]

    def test_deleted_excluded_by_default(self, temp_db):
        _create("to-delete")
        fs.write_feedback_entry(name="to-delete", action="delete", read_mark=0)
        result = fs.get_feedback_entries()
        assert result["entries"] == []

    def test_deleted_included_when_requested(self, temp_db):
        _create("to-delete")
        fs.write_feedback_entry(name="to-delete", action="delete", read_mark=0)
        result = fs.get_feedback_entries(include_deleted=True)
        assert len(result["entries"]) == 1
        assert result["entries"][0]["deleted_at"] is not None

    def test_notes_and_read_mark_reflect_added_notes(self, temp_db):
        _create("with-notes")
        fs.add_feedback_note(name="with-notes", kind="stumble", body="1回目の躓き")
        add_result = fs.add_feedback_note(name="with-notes", kind="note", body="補足")
        result = fs.get_feedback_entries(name="with-notes")
        entry = result["entries"][0]
        assert [n["kind"] for n in entry["notes"]] == ["stumble", "note"]
        assert entry["read_mark"] == add_result["read_mark"]


class TestWriteFeedbackEntryCreate:
    def test_create_notify_utterance_succeeds(self, temp_db):
        result = _create()
        assert result["ok"] is True
        assert result["entry"]["strength"] == "notify"
        assert result["entry"]["timing"] == "utterance"

    def test_create_block_pre_tool_succeeds(self, temp_db):
        result = _create(
            name="block-entry",
            strength="block",
            timing="pre_tool",
            condition={"tool": "Bash", "all": []},
        )
        assert result["ok"] is True
        assert result["entry"]["strength"] == "block"

    def test_invalid_action_rejected(self, temp_db):
        result = fs.write_feedback_entry(name="x", action="upsert")
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_name_pattern_rejected(self, temp_db):
        result = _create(name="Bad Name!")
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_duplicate_active_name_rejected(self, temp_db):
        _create("dup")
        result = _create("dup")
        assert result["ok"] is False
        assert result["error"]["code"] == "DUPLICATE"

    def test_empty_body_rejected(self, temp_db):
        result = _create(body="   ")
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_body_over_100_chars_rejected(self, temp_db):
        result = _create(body="a" * 101)
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_ref_over_500_chars_rejected(self, temp_db):
        result = _create(ref="a" * 501)
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_strength_timing_mismatch_rejected(self, temp_db):
        result = _create(strength="block", timing="utterance")
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_condition_regex_rejected(self, temp_db):
        result = _create(condition={"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "("}]})
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_block_with_no_tool_and_empty_all_rejected(self, temp_db):
        result = _create(strength="block", timing="pre_tool", condition={"tool": None, "all": []})
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_rejected_create_does_not_write_row(self, temp_db):
        _create("dup")
        _create("dup")  # DUPLICATE、書き込まれない
        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM feedback_entries WHERE name = 'dup'"
            ).fetchone()["n"]
        finally:
            conn.close()
        assert count == 1


class TestWriteFeedbackEntryRevival:
    def test_revival_requires_read_mark(self, temp_db):
        _create("revive-me")
        fs.write_feedback_entry(name="revive-me", action="delete", read_mark=0)
        result = fs.write_feedback_entry(
            name="revive-me",
            action="create",
            body="新しい本文",
            strength="notify",
            timing="utterance",
            condition={"tool": None, "all": []},
            read_mark=None,
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_revival_with_wrong_read_mark_rejected_as_conflict(self, temp_db):
        _create("revive-me")
        fs.write_feedback_entry(name="revive-me", action="delete", read_mark=0)
        result = fs.write_feedback_entry(
            name="revive-me",
            action="create",
            body="新しい本文",
            strength="notify",
            timing="utterance",
            condition={"tool": None, "all": []},
            read_mark=999,
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "CONFLICT"

    def test_revival_with_wrong_read_mark_and_invalid_body_rejected_as_conflict(self, temp_db):
        """read_markとbodyの両方が不正な入力では、内容検証より先にread_mark検証が
        走りCONFLICTを返す(_apply_updateと同じ優先順位。claude-reviewが指摘した
        変更経路間の非対称性の解消を確認する)。"""
        _create("revive-me")
        fs.write_feedback_entry(name="revive-me", action="delete", read_mark=0)
        result = fs.write_feedback_entry(
            name="revive-me",
            action="create",
            body="",  # 不正(非空文字列でない)
            strength="notify",
            timing="utterance",
            condition={"tool": None, "all": []},
            read_mark=999,  # 不正(古い)
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "CONFLICT"

    def test_revival_with_correct_read_mark_clears_deleted_at_and_keeps_notes(self, temp_db):
        _create("revive-me")
        note_result = fs.add_feedback_note(name="revive-me", kind="stumble", body="躓いた")
        del_result = fs.write_feedback_entry(
            name="revive-me", action="delete", read_mark=note_result["read_mark"]
        )
        assert del_result["ok"] is True
        current_mark = del_result["entry"]["read_mark"]
        result = fs.write_feedback_entry(
            name="revive-me",
            action="create",
            body="新しい本文",
            strength="notify",
            timing="utterance",
            condition={"tool": None, "all": []},
            read_mark=current_mark,
        )
        assert result["ok"] is True
        assert result["entry"]["deleted_at"] is None
        assert result["entry"]["body"] == "新しい本文"
        assert len(result["entry"]["notes"]) == 1  # ノートは引き継がれる

    def test_revival_keeps_delivered_and_overridden_counts(self, temp_db):
        _create("revive-me")
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE feedback_entries SET delivered_count = 5, overridden_count = 2 WHERE name = 'revive-me'"
            )
            conn.commit()
        finally:
            conn.close()
        fs.write_feedback_entry(name="revive-me", action="delete", read_mark=0)
        result = fs.write_feedback_entry(
            name="revive-me",
            action="create",
            body="新しい本文",
            strength="notify",
            timing="utterance",
            condition={"tool": None, "all": []},
            read_mark=0,
        )
        assert result["entry"]["delivered_count"] == 5
        assert result["entry"]["overridden_count"] == 2


class TestWriteFeedbackEntryUpdate:
    def test_update_with_correct_read_mark_succeeds(self, temp_db):
        _create("upd")
        result = fs.write_feedback_entry(
            name="upd",
            action="update",
            body="直した本文",
            strength="notify",
            timing="utterance",
            condition={"tool": None, "all": []},
            read_mark=0,
        )
        assert result["ok"] is True
        assert result["entry"]["body"] == "直した本文"

    def test_update_with_wrong_read_mark_rejected_as_conflict(self, temp_db):
        _create("upd")
        result = fs.write_feedback_entry(
            name="upd",
            action="update",
            body="直した本文",
            strength="notify",
            timing="utterance",
            condition={"tool": None, "all": []},
            read_mark=999,
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "CONFLICT"

    def test_update_with_wrong_read_mark_and_invalid_body_rejected_as_conflict(self, temp_db):
        """read_markとbodyの両方が不正な入力でもCONFLICTが優先される
        (revival側と同じ優先順位であることの確認)。"""
        _create("upd")
        result = fs.write_feedback_entry(
            name="upd",
            action="update",
            body="",  # 不正(非空文字列でない)
            strength="notify",
            timing="utterance",
            condition={"tool": None, "all": []},
            read_mark=999,  # 不正(古い)
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "CONFLICT"

    def test_update_after_reading_correct_mark_from_note_succeeds(self, temp_db):
        _create("upd")
        note_result = fs.add_feedback_note(name="upd", kind="note", body="経緯")
        result = fs.write_feedback_entry(
            name="upd",
            action="update",
            body="直した本文",
            strength="notify",
            timing="utterance",
            condition={"tool": None, "all": []},
            read_mark=note_result["read_mark"],
        )
        assert result["ok"] is True

    def test_update_nonexistent_name_rejected_as_not_found(self, temp_db):
        result = fs.write_feedback_entry(
            name="ghost",
            action="update",
            body="x",
            strength="notify",
            timing="utterance",
            condition={"tool": None, "all": []},
            read_mark=0,
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_update_deleted_entry_rejected_as_not_found(self, temp_db):
        _create("del-then-upd")
        fs.write_feedback_entry(name="del-then-upd", action="delete", read_mark=0)
        result = fs.write_feedback_entry(
            name="del-then-upd",
            action="update",
            body="x",
            strength="notify",
            timing="utterance",
            condition={"tool": None, "all": []},
            read_mark=0,
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_update_preserves_delivered_and_overridden_counts(self, temp_db):
        _create("upd")
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE feedback_entries SET delivered_count = 3, overridden_count = 1 WHERE name = 'upd'"
            )
            conn.commit()
        finally:
            conn.close()
        result = fs.write_feedback_entry(
            name="upd",
            action="update",
            body="直した本文",
            strength="notify",
            timing="utterance",
            condition={"tool": None, "all": []},
            read_mark=0,
        )
        assert result["entry"]["delivered_count"] == 3
        assert result["entry"]["overridden_count"] == 1


class TestWriteFeedbackEntryDelete:
    def test_delete_with_correct_read_mark_sets_deleted_at(self, temp_db):
        _create("del")
        result = fs.write_feedback_entry(name="del", action="delete", read_mark=0)
        assert result["ok"] is True
        assert result["entry"]["deleted_at"] is not None

    def test_delete_with_wrong_read_mark_rejected_as_conflict(self, temp_db):
        _create("del")
        result = fs.write_feedback_entry(name="del", action="delete", read_mark=999)
        assert result["ok"] is False
        assert result["error"]["code"] == "CONFLICT"

    def test_delete_nonexistent_name_rejected_as_not_found(self, temp_db):
        result = fs.write_feedback_entry(name="ghost", action="delete", read_mark=0)
        assert result["ok"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_delete_already_deleted_rejected_as_not_found(self, temp_db):
        _create("del-twice")
        fs.write_feedback_entry(name="del-twice", action="delete", read_mark=0)
        result = fs.write_feedback_entry(name="del-twice", action="delete", read_mark=0)
        assert result["ok"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_delete_keeps_notes(self, temp_db):
        _create("del-with-notes")
        fs.add_feedback_note(name="del-with-notes", kind="stumble", body="躓いた")
        result = fs.write_feedback_entry(name="del-with-notes", action="delete", read_mark=1)
        assert result["ok"] is True
        assert len(result["entry"]["notes"]) == 1


class TestAddFeedbackNote:
    def test_add_note_to_active_entry_succeeds(self, temp_db):
        _create("note-target")
        result = fs.add_feedback_note(name="note-target", kind="stumble", body="躓いた")
        assert result["ok"] is True
        assert result["note"]["kind"] == "stumble"
        assert result["read_mark"] == 1

    def test_add_note_does_not_require_read_mark_argument(self, temp_db):
        """add_feedback_noteはread_mark引数を取らない(いつでも書ける)。"""
        _create("note-target")
        result = fs.add_feedback_note(name="note-target", kind="note", body="経緯メモ")
        assert result["ok"] is True

    def test_add_note_to_deleted_entry_succeeds(self, temp_db):
        _create("deleted-target")
        fs.write_feedback_entry(name="deleted-target", action="delete", read_mark=0)
        result = fs.add_feedback_note(name="deleted-target", kind="note", body="削除後の観測")
        assert result["ok"] is True

    def test_add_note_to_nonexistent_name_rejected(self, temp_db):
        result = fs.add_feedback_note(name="ghost", kind="note", body="x")
        assert result["ok"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_invalid_kind_rejected(self, temp_db):
        _create("note-target")
        result = fs.add_feedback_note(name="note-target", kind="warning", body="x")
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_empty_body_rejected(self, temp_db):
        _create("note-target")
        result = fs.add_feedback_note(name="note-target", kind="note", body="   ")
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_body_over_500_chars_rejected(self, temp_db):
        _create("note-target")
        result = fs.add_feedback_note(name="note-target", kind="note", body="a" * 501)
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_notes_accumulate_and_read_mark_increases(self, temp_db):
        _create("note-target")
        r1 = fs.add_feedback_note(name="note-target", kind="stumble", body="1回目")
        r2 = fs.add_feedback_note(name="note-target", kind="stumble", body="2回目")
        assert r2["read_mark"] > r1["read_mark"]

    def test_no_hint_key_when_pending_stumbles_below_threshold(self, temp_db):
        """hintが無いときのadd_feedback_noteの戻り値は、従来と同じ形(keyが3つのみ)。"""
        _create("note-target")
        r1 = fs.add_feedback_note(name="note-target", kind="stumble", body="1回目")
        assert set(r1) == {"ok", "note", "read_mark"}
        r2 = fs.add_feedback_note(name="note-target", kind="stumble", body="2回目")
        assert set(r2) == {"ok", "note", "read_mark"}

    def test_hint_key_appears_on_3rd_pending_stumble(self, temp_db):
        _create("note-target")
        fs.add_feedback_note(name="note-target", kind="stumble", body="1回目")
        fs.add_feedback_note(name="note-target", kind="stumble", body="2回目")
        r3 = fs.add_feedback_note(name="note-target", kind="stumble", body="3回目")
        assert "hint" in r3
        assert "未処理の躓き3件" in r3["hint"]

    def test_hint_key_absent_when_note_added(self, temp_db):
        """noteを足す呼び出し自体ではhintは付かない(pending stumblesの計算はnote挿入後を基準にする)。"""
        _create("note-target")
        fs.add_feedback_note(name="note-target", kind="stumble", body="1回目")
        fs.add_feedback_note(name="note-target", kind="stumble", body="2回目")
        fs.add_feedback_note(name="note-target", kind="stumble", body="3回目")
        r_note = fs.add_feedback_note(name="note-target", kind="note", body="対応した")
        assert "hint" not in r_note
