"""skills/ask-watch/scripts/poll.py の単体テスト。

ディレクトリ名にハイフンを含む(ask-watch)ため通常のdotted importができず、
importlib.util.spec_from_file_locationでファイルパスから直接読み込む。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

_POLL_PATH = Path(__file__).resolve().parents[2] / "skills" / "ask-watch" / "scripts" / "poll.py"


def _load_poll_module():
    spec = importlib.util.spec_from_file_location("ask_watch_poll", _POLL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(tmp_path: Path) -> str:
    db_path = tmp_path / "discussion.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE asks (id INTEGER PRIMARY KEY, status TEXT, last_seen_at TEXT)")
    conn.commit()
    conn.close()
    return str(db_path)


def test_snapshot_changes_when_new_open_ask_added(tmp_path):
    poll = _load_poll_module()
    db_path = _make_db(tmp_path)

    before = poll._snapshot(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO asks (id, status, last_seen_at) VALUES (1, 'open', '2026-01-01')")
    conn.commit()
    conn.close()

    after = poll._snapshot(db_path)

    assert before != after
    assert after == (1, "2026-01-01", "1")


def test_snapshot_ignores_non_open_asks(tmp_path):
    """statusがopen以外のaskは件数・id集合に含めない。"""
    poll = _load_poll_module()
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO asks (id, status, last_seen_at) VALUES (1, 'answered', '2026-01-01')")
    conn.commit()
    conn.close()

    assert poll._snapshot(db_path) == (0, None, None)


def test_snapshot_detects_id_set_change_with_same_count(tmp_path):
    """件数が同じでもid構成が入れ替わる変化(1件closeして1件openになった等)を取りこぼさない。"""
    poll = _load_poll_module()
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO asks (id, status, last_seen_at) VALUES (1, 'open', '2026-01-01')")
    conn.commit()
    conn.close()
    before = poll._snapshot(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE asks SET status='answered' WHERE id=1")
    conn.execute("INSERT INTO asks (id, status, last_seen_at) VALUES (2, 'open', '2026-01-02')")
    conn.commit()
    conn.close()
    after = poll._snapshot(db_path)

    assert before[0] == after[0] == 1  # 件数は変わらない
    assert before != after  # だがid集合は変わっている


def test_snapshot_returns_none_on_query_failure(tmp_path):
    """テーブル未作成等でクエリに失敗した場合はNoneを返す(このtickをスキップする)。"""
    poll = _load_poll_module()
    db_path = str(tmp_path / "no_such_table.db")
    sqlite3.connect(db_path).close()  # ファイルだけ存在させる(asksテーブルは無い)

    assert poll._snapshot(db_path) is None
