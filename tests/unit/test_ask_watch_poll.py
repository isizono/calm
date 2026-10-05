"""skills/ask-watch/scripts/poll.py の単体テスト。

ディレクトリ名にハイフンを含む(ask-watch)ため通常のdotted importができず、
importlib.util.spec_from_file_locationでファイルパスから直接読み込む。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

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
    """件数・MAX(last_seen_at)が同じでもid構成が入れ替わる変化
    (1件closeして1件openになった等)を取りこぼさない。

    新規openのlast_seen_atを既存の最大値と同じ値にすることで、
    MAX(last_seen_at)の変化に頼らずid集合の変化だけで検知できることを
    確かめる(id=1,3→id=1,2のように最小値側のidを変えないことで、
    GROUP_CONCAT(id)をCAST(MIN(id) AS TEXT)に弱めても偶然一致しない
    組み合わせにしている)。
    """
    poll = _load_poll_module()
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO asks (id, status, last_seen_at) VALUES (1, 'open', '2026-01-05')")
    conn.execute("INSERT INTO asks (id, status, last_seen_at) VALUES (3, 'open', '2026-01-05')")
    conn.commit()
    conn.close()
    before = poll._snapshot(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE asks SET status='answered' WHERE id=3")
    conn.execute("INSERT INTO asks (id, status, last_seen_at) VALUES (2, 'open', '2026-01-05')")
    conn.commit()
    conn.close()
    after = poll._snapshot(db_path)

    assert before == (2, "2026-01-05", "1,3")
    assert after == (2, "2026-01-05", "1,2")
    assert before[0] == after[0]  # 件数は変わらない
    assert before[1] == after[1]  # MAX(last_seen_at)も変わらない
    assert before != after  # だがid集合は変わっている


def test_snapshot_returns_none_on_query_failure(tmp_path):
    """テーブル未作成等でクエリに失敗した場合はNoneを返す(このtickをスキップする)。"""
    poll = _load_poll_module()
    db_path = str(tmp_path / "no_such_table.db")
    sqlite3.connect(db_path).close()  # ファイルだけ存在させる(asksテーブルは無い)

    assert poll._snapshot(db_path) is None


def test_snapshot_returns_none_on_corrupt_database_file(tmp_path):
    """sqlite3.OperationalErrorだけでなくsqlite3.DatabaseError(壊れたファイル・
    復元コピー途中等)も拾ってNoneを返す(拾わないとポーラー自体が落ちて監視が
    止まる)。"""
    poll = _load_poll_module()
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"not a sqlite database, just garbage bytes")

    assert poll._snapshot(str(db_path)) is None


def test_snapshot_does_not_create_file_when_db_path_missing(tmp_path):
    """sqlite3.connect()はファイルが無いと新規作成してしまうため、復元中で
    DBファイルが一時的に無い間に空ファイルを作ってしまわないことを確かめる。"""
    poll = _load_poll_module()
    db_path = tmp_path / "not_yet_created.db"

    assert poll._snapshot(str(db_path)) is None
    assert not db_path.exists()


def test_main_skips_tick_on_query_failure_without_resetting_prev(monkeypatch, capsys):
    """_snapshotが一時的にNoneを返した回(クエリ失敗)はprevを巻き戻さない。

    Noneを巻き戻し対象にすると、クエリ失敗の瞬間に変化扱いで誤発火したり、
    直後に元の値へ戻っただけなのに再度変化扱いで誤発火したりする。

    並びは A, B, B, None, B (Aが初期prev)。B続きの2回目とNoneの後のBは
    「1回の変化につき1行だけ出す」契約により出力されないはずで、
    `prev = cur` を消す変異(変化を出した後も毎tickprevが更新されず、
    同じ変化を出し続ける)が混じると、この2回でも誤って出力されてしまう。
    """
    poll = _load_poll_module()
    monkeypatch.setattr(poll.time, "sleep", lambda _: None)
    monkeypatch.setattr(poll, "get_db_path", lambda: "unused")

    snapshot_a = (1, "2026-01-01", "1")
    snapshot_b = (2, "2026-01-02", "1,2")
    snapshots = iter([snapshot_a, snapshot_b, snapshot_b, None, snapshot_b])

    def fake_snapshot(db_path):
        try:
            return next(snapshots)
        except StopIteration as exc:
            raise RuntimeError("stop loop") from exc

    monkeypatch.setattr(poll, "_snapshot", fake_snapshot)

    with pytest.raises(RuntimeError, match="stop loop"):
        poll.main()

    out = capsys.readouterr().out
    assert out.count("ask store changed") == 1
    assert f"ask store changed: {snapshot_b}" in out
