"""per-session inbox（src/services/relay/inbox.py）の unit test。

drain の契約: 既読分を返さない / 未読が無ければ空 / 破損行はスキップ /
書きかけ行は持ち越す / 読み切ったら file を切り詰める（at-least-once 前提）。
peek=True は cursor・file を変更しない非破壊読み取り、has_more は未消費バイトの有無を示す。
"""
import json

import pytest

from src.services.relay import inbox


@pytest.fixture(autouse=True)
def relay_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    return tmp_path / "relay-state"


class TestDrainEmpty:
    def test_missing_inbox_returns_empty_list(self):
        assert inbox.drain("never-subscribed")["records"] == []

    def test_fully_drained_inbox_returns_empty_list(self):
        inbox.append("s1", {"n": 1})
        assert len(inbox.drain("s1")["records"]) == 1
        assert inbox.drain("s1")["records"] == []


class TestDrainCursor:
    def test_second_drain_returns_only_new_records(self):
        inbox.append("s1", {"n": 1})
        inbox.append("s1", {"n": 2})
        first = inbox.drain("s1")["records"]
        assert [r["n"] for r in first] == [1, 2]

        inbox.append("s1", {"n": 3})
        second = inbox.drain("s1")["records"]
        assert [r["n"] for r in second] == [3]

    def test_limit_stops_midway_and_resumes(self):
        for n in range(3):
            inbox.append("s1", {"n": n})
        first = inbox.drain("s1", limit=2)["records"]
        assert [r["n"] for r in first] == [0, 1]
        second = inbox.drain("s1")["records"]
        assert [r["n"] for r in second] == [2]

    def test_full_drain_truncates_file_and_resets_cursor(self):
        inbox.append("s1", {"n": 1})
        inbox.drain("s1")
        assert inbox.inbox_path("s1").stat().st_size == 0
        assert inbox.read_cursor("s1") == 0

    def test_cursor_beyond_file_size_rereads_from_start(self):
        inbox.append("s1", {"n": 1})
        inbox._write_cursor("s1", 10_000)
        assert [r["n"] for r in inbox.drain("s1")["records"]] == [1]


class TestDrainRobustness:
    def test_malformed_line_is_skipped(self):
        inbox.append("s1", {"n": 1})
        with open(inbox.inbox_path("s1"), "ab") as f:
            f.write(b"{broken json\n")
        inbox.append("s1", {"n": 2})
        assert [r["n"] for r in inbox.drain("s1")["records"]] == [1, 2]

    def test_partial_trailing_line_is_not_consumed(self):
        inbox.append("s1", {"n": 1})
        with open(inbox.inbox_path("s1"), "ab") as f:
            f.write(b'{"n": 2')
        assert [r["n"] for r in inbox.drain("s1")["records"]] == [1]

        # 書き手が行を完成させたら次の drain で読める
        with open(inbox.inbox_path("s1"), "ab") as f:
            f.write(b"}\n")
        assert [r["n"] for r in inbox.drain("s1")["records"]] == [2]

    def test_append_writes_one_json_line_per_record(self):
        inbox.append("s1", {"a": 1})
        inbox.append("s1", {"b": 2})
        lines = inbox.inbox_path("s1").read_bytes().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"a": 1}


class TestDrainPeek:
    def test_peek_does_not_advance_cursor(self):
        inbox.append("s1", {"n": 1})
        inbox.append("s1", {"n": 2})
        result = inbox.drain("s1", peek=True)
        assert [r["n"] for r in result["records"]] == [1, 2]
        assert inbox.read_cursor("s1") == 0

    def test_peek_does_not_truncate_file(self):
        inbox.append("s1", {"n": 1})
        size_before = inbox.inbox_path("s1").stat().st_size
        inbox.drain("s1", peek=True)
        assert inbox.inbox_path("s1").stat().st_size == size_before

    def test_repeated_peek_returns_same_records(self):
        inbox.append("s1", {"n": 1})
        first = inbox.drain("s1", peek=True)["records"]
        second = inbox.drain("s1", peek=True)["records"]
        assert first == second == [{"n": 1}]

    def test_peek_then_consume_reads_same_records_and_advances(self):
        inbox.append("s1", {"n": 1})
        peeked = inbox.drain("s1", peek=True)["records"]
        consumed = inbox.drain("s1")["records"]
        assert peeked == consumed == [{"n": 1}]
        # consume 後は既読化されており、以降は空
        assert inbox.drain("s1")["records"] == []

    def test_new_arrival_between_peek_and_consume_is_included_in_consume(self):
        inbox.append("s1", {"n": 1})
        inbox.drain("s1", peek=True)
        inbox.append("s1", {"n": 2})
        consumed = inbox.drain("s1")["records"]
        assert [r["n"] for r in consumed] == [1, 2]


class TestDrainHasMore:
    def test_has_more_true_when_limit_cuts_off_remaining_records(self):
        for n in range(3):
            inbox.append("s1", {"n": n})
        result = inbox.drain("s1", limit=2)
        assert result["has_more"] is True

    def test_has_more_false_when_all_unread_consumed(self):
        inbox.append("s1", {"n": 1})
        result = inbox.drain("s1")
        assert result["has_more"] is False

    def test_has_more_false_on_missing_inbox(self):
        assert inbox.drain("never-subscribed")["has_more"] is False

    def test_has_more_true_on_partial_trailing_line(self):
        inbox.append("s1", {"n": 1})
        with open(inbox.inbox_path("s1"), "ab") as f:
            f.write(b'{"n": 2')
        result = inbox.drain("s1")
        assert result["has_more"] is True


class TestCountUnread:
    def test_missing_inbox_returns_zero(self):
        assert inbox.count_unread("never-subscribed") == 0

    def test_fully_drained_inbox_returns_zero(self):
        inbox.append("s1", {"n": 1})
        inbox.drain("s1")
        assert inbox.count_unread("s1") == 0

    def test_counts_unread_records(self):
        inbox.append("s1", {"n": 1})
        inbox.append("s1", {"n": 2})
        inbox.append("s1", {"n": 3})
        assert inbox.count_unread("s1") == 3

    def test_counts_only_records_after_cursor(self):
        inbox.append("s1", {"n": 1})
        inbox.drain("s1")
        inbox.append("s1", {"n": 2})
        inbox.append("s1", {"n": 3})
        assert inbox.count_unread("s1") == 2

    def test_partial_trailing_line_is_not_counted(self):
        inbox.append("s1", {"n": 1})
        with open(inbox.inbox_path("s1"), "ab") as f:
            f.write(b'{"n": 2')
        assert inbox.count_unread("s1") == 1

    def test_does_not_advance_cursor(self):
        inbox.append("s1", {"n": 1})
        inbox.append("s1", {"n": 2})
        cursor_before = inbox.read_cursor("s1")
        assert inbox.count_unread("s1") == 2
        assert inbox.read_cursor("s1") == cursor_before
        # drain後も同じ2件が読めることでcursorが前進していないことを確認する
        assert [r["n"] for r in inbox.drain("s1")["records"]] == [1, 2]

    def test_cursor_beyond_file_size_rereads_from_start(self):
        inbox.append("s1", {"n": 1})
        inbox._write_cursor("s1", 10_000)
        assert inbox.count_unread("s1") == 1

    def test_malformed_line_is_not_counted(self):
        """drain()が実際に返す件数と一致させるため、壊れた行はcountに含めない"""
        inbox.append("s1", {"n": 1})
        with open(inbox.inbox_path("s1"), "ab") as f:
            f.write(b"{broken json\n")
        inbox.append("s1", {"n": 2})
        assert inbox.count_unread("s1") == 2
        assert [r["n"] for r in inbox.drain("s1")["records"]] == [1, 2]


class TestEnsureInboxFile:
    def test_creates_missing_file(self):
        assert not inbox.inbox_path("s1").exists()
        inbox.ensure_inbox_file("s1")
        assert inbox.inbox_path("s1").exists()

    def test_creates_parent_directory(self, tmp_path):
        assert not inbox.inbox_path("s1").parent.exists()
        inbox.ensure_inbox_file("s1")
        assert inbox.inbox_path("s1").parent.is_dir()

    def test_returns_inbox_path(self):
        assert inbox.ensure_inbox_file("s1") == inbox.inbox_path("s1")

    def test_does_not_truncate_existing_content(self):
        inbox.append("s1", {"n": 1})
        inbox.ensure_inbox_file("s1")
        assert [r["n"] for r in inbox.drain("s1")["records"]] == [1]

    def test_does_not_reset_cursor_of_existing_file(self):
        inbox.append("s1", {"n": 1})
        inbox.append("s1", {"n": 2})
        inbox.drain("s1", limit=1)
        inbox.ensure_inbox_file("s1")
        assert [r["n"] for r in inbox.drain("s1")["records"]] == [2]


class TestListInboxFiles:
    def test_returns_empty_list_when_dir_missing(self):
        assert inbox.list_inbox_files() == []

    def test_returns_session_id_and_path_pairs(self):
        inbox.append("s1", {"n": 1})
        inbox.ensure_inbox_file("s2")
        result = dict(inbox.list_inbox_files())
        assert set(result.keys()) == {"s1", "s2"}
        assert result["s1"] == inbox.inbox_path("s1")
        assert result["s2"] == inbox.inbox_path("s2")

    def test_ignores_cursor_files(self):
        inbox.append("s1", {"n": 1})
        inbox.drain("s1", limit=0)
        assert inbox.cursor_path("s1").exists()
        result = dict(inbox.list_inbox_files())
        assert list(result.keys()) == ["s1"]
