"""citation_renderer.expand / adjust_snippet_boundary の単体テスト"""
import sqlite3
import pytest

from src.services.citation_renderer import (
    adjust_snippet_boundary,
    apply_flavor_to_snippet,
    expand,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE materials (id INTEGER PRIMARY KEY, title TEXT, content TEXT, retracted_at TEXT);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY, decision TEXT, reason TEXT, title TEXT, retracted_at TEXT);
        CREATE TABLE discussion_logs (id INTEGER PRIMARY KEY, title TEXT, content TEXT, retracted_at TEXT);
        CREATE TABLE activities (id INTEGER PRIMARY KEY, title TEXT, description TEXT);
        CREATE TABLE discussion_topics (id INTEGER PRIMARY KEY, title TEXT, description TEXT);
        CREATE TABLE citations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_type TEXT, owner_id INTEGER,
            target_type TEXT, target_id INTEGER,
            occurrence INTEGER,
            UNIQUE(owner_type, owner_id, occurrence)
        );
        INSERT INTO materials VALUES (1, 'Design v3', 'body', NULL);
        INSERT INTO decisions VALUES (10, 'use SQLite', 'simplicity', NULL, NULL);
        INSERT INTO decisions VALUES (11, 'old choice', 'reason', NULL, '2026-01-01');
        INSERT INTO discussion_logs VALUES (100, 'kickoff', 'content here', NULL);
        INSERT INTO activities VALUES (50, 'sprint plan', 'goals');
        INSERT INTO discussion_topics VALUES (200, 'main topic', 'description');
        """
    )
    yield c
    c.close()


class TestFlavorExpand:
    def test_raw_unchanged(self, conn):
        text = "See {{cite:M#1}} and {{cite:D#10}}."
        assert expand(text, "raw", conn) == text

    def test_internal_with_id(self, conn):
        text = "See {{cite:M#1}} and {{cite:D#10}}."
        out = expand(text, "internal", conn)
        assert "Design v3 (M#1)" in out
        assert "use SQLite (D#10)" in out

    def test_readable_no_id(self, conn):
        text = "See {{cite:M#1}} and {{cite:D#10}}."
        out = expand(text, "readable", conn)
        assert "Design v3" in out
        assert "(M#1)" not in out
        assert "(D#10)" not in out

    def test_deleted_target_internal(self, conn):
        out = expand("missing {{cite:M#999}} here", "internal", conn)
        assert "[deleted M#999]" in out

    def test_deleted_target_readable(self, conn):
        out = expand("missing {{cite:M#999}} here", "readable", conn)
        assert "[deleted item]" in out

    def test_retracted_target_internal(self, conn):
        out = expand("see {{cite:D#11}}.", "internal", conn)
        assert "[retracted D#11]" in out

    def test_retracted_target_readable(self, conn):
        out = expand("see {{cite:D#11}}.", "readable", conn)
        assert "[retracted item]" in out

    def test_un_retract_dynamic(self, conn):
        # retract → un-retract で通常表示に戻る
        out1 = expand("{{cite:D#11}}", "internal", conn)
        assert "[retracted" in out1
        conn.execute("UPDATE decisions SET retracted_at = NULL WHERE id = 11")
        out2 = expand("{{cite:D#11}}", "internal", conn)
        assert "[retracted" not in out2
        assert "old choice" in out2

    def test_code_block_not_expanded(self, conn):
        text = "before {{cite:M#1}}\n```\n{{cite:M#1}}\n```\nafter {{cite:M#1}}"
        out = expand(text, "internal", conn)
        # 3 つ中、code block 内の 1 つだけ無加工
        assert out.count("Design v3 (M#1)") == 2
        assert "{{cite:M#1}}" in out  # 元のまま残る

    def test_escape_not_expanded(self, conn):
        text = r"escape \{{cite:M#1}} only"
        out = expand(text, "internal", conn)
        assert out == text  # 展開しない

    def test_inline_backtick_not_expanded(self, conn):
        text = "raw `{{cite:M#1}}` and live {{cite:M#1}}"
        out = expand(text, "internal", conn)
        assert "`{{cite:M#1}}`" in out
        assert "Design v3 (M#1)" in out

    def test_none_input_returns_empty_string(self, conn):
        assert expand(None, "internal", conn) == ""
        assert expand(None, "readable", conn) == ""
        assert expand(None, "raw", conn) == ""


class TestSnippetBoundary:
    def test_truncated_open_template_at_end(self):
        s = "before {{cite:M#"
        assert adjust_snippet_boundary(s) == "before "

    def test_full_template_preserved(self):
        s = "before {{cite:M#1}} after"
        assert adjust_snippet_boundary(s) == s

    def test_truncated_close_at_start(self):
        s = "M#1}} after"
        assert adjust_snippet_boundary(s) == " after"

    def test_no_templates_unchanged(self):
        s = "just plain text"
        assert adjust_snippet_boundary(s) == s


class TestApplyFlavorToSnippet:
    def test_raw_no_change(self, conn):
        s = "See {{cite:M#1}} ext"
        assert apply_flavor_to_snippet(s, "raw", conn) == s

    def test_internal_adjusts_and_expands(self, conn):
        s = "See {{cite:M#1}} ext"
        out = apply_flavor_to_snippet(s, "internal", conn)
        assert "Design v3 (M#1)" in out

    def test_truncated_template_dropped(self, conn):
        s = "See {{cite:M#"
        out = apply_flavor_to_snippet(s, "internal", conn)
        assert out == "See "

    def test_none_input_returns_empty_string(self, conn):
        assert apply_flavor_to_snippet(None, "internal", conn) == ""
        assert apply_flavor_to_snippet(None, "readable", conn) == ""
        assert apply_flavor_to_snippet(None, "raw", conn) == ""
