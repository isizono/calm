"""per-session inbox（src/services/relay/inbox.py）の unit test。

drain の契約: 既読分を返さない / 未読が無ければ空 / 破損行はスキップ /
書きかけ行は持ち越す / 読み切ったら file を切り詰める（at-least-once 前提）。
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
        assert inbox.drain("never-subscribed") == []

    def test_fully_drained_inbox_returns_empty_list(self):
        inbox.append("s1", {"n": 1})
        assert len(inbox.drain("s1")) == 1
        assert inbox.drain("s1") == []


class TestDrainCursor:
    def test_second_drain_returns_only_new_records(self):
        inbox.append("s1", {"n": 1})
        inbox.append("s1", {"n": 2})
        first = inbox.drain("s1")
        assert [r["n"] for r in first] == [1, 2]

        inbox.append("s1", {"n": 3})
        second = inbox.drain("s1")
        assert [r["n"] for r in second] == [3]

    def test_limit_stops_midway_and_resumes(self):
        for n in range(3):
            inbox.append("s1", {"n": n})
        first = inbox.drain("s1", limit=2)
        assert [r["n"] for r in first] == [0, 1]
        second = inbox.drain("s1")
        assert [r["n"] for r in second] == [2]

    def test_full_drain_truncates_file_and_resets_cursor(self):
        inbox.append("s1", {"n": 1})
        inbox.drain("s1")
        assert inbox.inbox_path("s1").stat().st_size == 0
        assert inbox.read_cursor("s1") == 0

    def test_cursor_beyond_file_size_rereads_from_start(self):
        inbox.append("s1", {"n": 1})
        inbox._write_cursor("s1", 10_000)
        assert [r["n"] for r in inbox.drain("s1")] == [1]


class TestDrainRobustness:
    def test_malformed_line_is_skipped(self):
        inbox.append("s1", {"n": 1})
        with open(inbox.inbox_path("s1"), "ab") as f:
            f.write(b"{broken json\n")
        inbox.append("s1", {"n": 2})
        assert [r["n"] for r in inbox.drain("s1")] == [1, 2]

    def test_partial_trailing_line_is_not_consumed(self):
        inbox.append("s1", {"n": 1})
        with open(inbox.inbox_path("s1"), "ab") as f:
            f.write(b'{"n": 2')
        assert [r["n"] for r in inbox.drain("s1")] == [1]

        # 書き手が行を完成させたら次の drain で読める
        with open(inbox.inbox_path("s1"), "ab") as f:
            f.write(b"}\n")
        assert [r["n"] for r in inbox.drain("s1")] == [2]

    def test_append_writes_one_json_line_per_record(self):
        inbox.append("s1", {"a": 1})
        inbox.append("s1", {"b": 2})
        lines = inbox.inbox_path("s1").read_bytes().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"a": 1}


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
        assert [r["n"] for r in inbox.drain("s1")] == [1, 2]

    def test_cursor_beyond_file_size_rereads_from_start(self):
        inbox.append("s1", {"n": 1})
        inbox._write_cursor("s1", 10_000)
        assert inbox.count_unread("s1") == 1
