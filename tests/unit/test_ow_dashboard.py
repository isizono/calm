"""src/services/ow/dashboard のユニットテスト。

ダッシュボード レンダラの出力形式が M#288 §6.0 スタイル指針（行幅100以内、カラー
コード未出力、絵文字制限、UTF-8 LF、Markdown valid）を満たし、extract_activity_line
が render 結果から activity_id 行を抜き出せ、`.views/dashboard-t<topic>.md` が
atomic write されることを検証する。
"""
import os
import re
import tempfile
from pathlib import Path

import pytest

from src.db import get_connection, init_database
from src.services.ow import channels as ch
from src.services.ow.dashboard import (
    extract_activity_line,
    is_ow_managed_activity_with_conn,
    render_dashboard,
    render_with_conn,
    view_file_path,
    write_view_file_atomic,
)
from src.services.tag_service import ensure_tag_ids, link_tags


NOW = "2026-06-17T10:00:00Z"
CH = "C1"


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        os.environ["OW_VIEWS_DIR"] = os.path.join(tmpdir, "views")
        init_database()
        yield db_path
        for k in ("DISCUSSION_DB_PATH", "OW_VIEWS_DIR"):
            if k in os.environ:
                del os.environ[k]


def _make_topic(conn) -> int:
    cur = conn.execute(
        "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
        ("t", "d"),
    )
    return cur.lastrowid


def _make_activity(
    conn, *, title="アクティビティ", status="pending", with_ow_managed=True,
    topic_id=None, intent: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
        (title, "", status),
    )
    aid = cur.lastrowid
    if with_ow_managed:
        tag_ids = ensure_tag_ids(conn, [("ow", "managed")])
        link_tags(conn, "activity_tags", "activity_id", aid, tag_ids)
    if intent:
        tag_ids = ensure_tag_ids(conn, [("intent", intent)])
        link_tags(conn, "activity_tags", "activity_id", aid, tag_ids)
    if topic_id is not None:
        conn.execute(
            "INSERT INTO relations (source_type, source_id, target_type, target_id) "
            "VALUES ('activity', ?, 'topic', ?)",
            (aid, topic_id),
        )
    return aid


def _insert_worker(
    conn, *, alias, activity_id, topic_id, task_n,
    workload_state="working", last_heartbeat_at=None,
    terminated_at=None, cause=None,
):
    conn.execute(
        """
        INSERT INTO ow_workers
          (channel_code, handle, alias, activity_id, topic_id, task_n,
           workload_state, cause, last_heartbeat_at, terminated_at, spawned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (CH, alias, alias, activity_id, topic_id, task_n,
         workload_state, cause, last_heartbeat_at, terminated_at, NOW),
    )


class TestRenderBasic:
    def test_empty_topic_renders_placeholder(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code=CH, topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            text = render_with_conn(conn, topic_id=tid, role="general", now_iso=NOW)
            assert "アクティビティ一覧" in text
            assert "ow:managed の activity がありません" in text
        finally:
            conn.close()

    def test_single_activity_no_worker_renders_pending(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code=CH, topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            aid = _make_activity(conn, intent="implement", topic_id=tid)
            text = render_with_conn(conn, topic_id=tid, role="general", now_iso=NOW)
            line = extract_activity_line(text, aid)
            assert line is not None
            assert "◐" in line
            assert f"[{aid}]" in line
            assert "[作業]" in line
            assert "pending" in line
        finally:
            conn.close()

    def test_activity_with_alive_worker_shows_presence(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code=CH, topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            aid = _make_activity(
                conn, intent="implement", status="in_progress", topic_id=tid,
            )
            _insert_worker(
                conn, alias="w-z", activity_id=aid, topic_id=tid, task_n=1,
                workload_state="working",
                last_heartbeat_at="2026-06-17T09:59:57Z",
            )
            text = render_with_conn(conn, topic_id=tid, role="general", now_iso=NOW)
            line = extract_activity_line(text, aid)
            assert line is not None
            assert "●" in line
            assert "in_progress" in line
            assert "w-z" in line
            assert "working" in line
            assert "hb 3s ago" in line
        finally:
            conn.close()


class TestStyleCompliance:
    """M#288 §6.0 スタイル指針への適合性"""

    def test_no_color_escape_codes(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code=CH, topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            _make_activity(conn, intent="implement", topic_id=tid)
            text = render_with_conn(conn, topic_id=tid, role="general", now_iso=NOW)
            assert "\x1b[" not in text
        finally:
            conn.close()

    def test_no_box_drawing_chars(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code=CH, topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            _make_activity(conn, intent="implement", topic_id=tid)
            text = render_with_conn(conn, topic_id=tid, role="general", now_iso=NOW)
            forbidden = "┌┐└┘─│├┤┬┴┼"
            # `─` は U+2500 box-drawing だが、§6.0 でセパレータとして使用可と
            # ガイドにある。それ以外の box-drawing 文字は出さない。
            for ch_ in forbidden.replace("─", ""):
                assert ch_ not in text, f"box-drawing文字 {ch_!r} が出力された"
        finally:
            conn.close()

    def test_each_line_within_max_width(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code=CH, topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            # 非常に長いタイトル
            _make_activity(
                conn, title="あ" * 200, intent="implement", topic_id=tid,
            )
            text = render_with_conn(conn, topic_id=tid, role="general", now_iso=NOW)
            # 文字数ベースの素朴チェック。半角換算で 100 以内になっているか
            # （全角は 2 換算で _truncate しているため、各行 100 文字以内）
            for line in text.splitlines():
                width = sum(2 if ord(c) > 0x7F else 1 for c in line)
                assert width <= 100, f"行幅オーバー (width={width}): {line!r}"
        finally:
            conn.close()


class TestExtractActivityLine:
    def test_extract_returns_matching_line(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code=CH, topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            a1 = _make_activity(conn, title="A1", intent="implement", topic_id=tid)
            a2 = _make_activity(conn, title="A2", intent="implement", topic_id=tid)
            text = render_with_conn(conn, topic_id=tid, role="general", now_iso=NOW)
            line1 = extract_activity_line(text, a1)
            line2 = extract_activity_line(text, a2)
            assert line1 is not None and f"[{a1}]" in line1
            assert line2 is not None and f"[{a2}]" in line2
        finally:
            conn.close()

    def test_extract_returns_none_when_not_present(self):
        text = "## ヘッダ\n（empty）\n"
        assert extract_activity_line(text, 999) is None

    def test_extract_ignores_substring_id_matches(self):
        text = (
            "## a\n"
            "● [12] [作業] 部分一致テスト ─ pending\n"
            "● [123] [作業] 全体一致テスト ─ pending\n"
        )
        line = extract_activity_line(text, 12)
        assert line is not None
        assert "[12]" in line
        assert "[123]" not in line


class TestViewFileWrite:
    def test_atomic_write_creates_file_and_dir(self, db):
        tid = 999
        content = "## test\n● [1] [作業] x ─ pending\n"
        path = write_view_file_atomic(tid, content)
        assert path.exists()
        assert path.read_text(encoding="utf-8") == content
        assert path == view_file_path(tid)

    def test_atomic_write_overwrites_existing(self, db):
        tid = 998
        write_view_file_atomic(tid, "old content\n")
        write_view_file_atomic(tid, "new content\n")
        assert view_file_path(tid).read_text(encoding="utf-8") == "new content\n"

    def test_no_leftover_tmp_after_write(self, db):
        tid = 997
        path = write_view_file_atomic(tid, "content\n")
        leftovers = [
            p for p in path.parent.iterdir()
            if p.name.startswith(f"dashboard-t{tid}.") and p.suffix == ".tmp"
        ]
        assert leftovers == []


class TestRenderDashboardEntry:
    def test_render_dashboard_writes_view_file(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code=CH, topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            _make_activity(conn, intent="implement", topic_id=tid)
            conn.commit()
        finally:
            conn.close()
        result = render_dashboard(
            topic_id=tid, role="general", apply_state=False, now_iso=NOW,
        )
        assert "rendered" in result
        assert "アクティビティ一覧" in result["rendered"]
        assert result["view_file"] is not None
        assert Path(result["view_file"]).exists()

    def test_render_dashboard_no_view_when_disabled(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code=CH, topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            _make_activity(conn, intent="implement", topic_id=tid)
            conn.commit()
        finally:
            conn.close()
        result = render_dashboard(
            topic_id=tid, role="general", write_view=False,
            apply_state=False, now_iso=NOW,
        )
        assert result["view_file"] is None


class TestRoleOrch:
    def test_orch_role_includes_alive_workers_section(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code=CH, topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            aid = _make_activity(conn, intent="implement", topic_id=tid)
            _insert_worker(
                conn, alias="w-z", activity_id=aid, topic_id=tid, task_n=1,
                workload_state="working",
                last_heartbeat_at="2026-06-17T09:59:55Z",
            )
            text = render_with_conn(conn, topic_id=tid, role="orch", now_iso=NOW)
            assert "## orch詳細ビュー" in text
            assert "### Alive workers" in text
            assert "w-z" in text
        finally:
            conn.close()


class TestIsOwManaged:
    def test_returns_true_for_ow_managed(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            aid = _make_activity(conn, topic_id=tid)
            assert is_ow_managed_activity_with_conn(conn, aid) is True
        finally:
            conn.close()

    def test_returns_false_for_unmanaged(self, db):
        conn = get_connection()
        try:
            aid = _make_activity(conn, with_ow_managed=False)
            assert is_ow_managed_activity_with_conn(conn, aid) is False
        finally:
            conn.close()
