"""ask_notify.py の単体テスト。

notify_pathの生成規則、write_notificationの追記内容、TTLスイープ、書き込み
失敗時に例外を上げないことを検証する。
"""
import json
import os
import time

import pytest

from src.services import ask_notify


@pytest.fixture
def notify_dir(tmp_path, monkeypatch):
    """CALM_ASK_NOTIFY_DIRをtmp_path配下に切り替える。"""
    d = tmp_path / "ask-notify"
    monkeypatch.setenv("CALM_ASK_NOTIFY_DIR", str(d))
    return d


class TestNotifyPath:
    def test_notify_dir_honors_env_override(self, notify_dir):
        assert ask_notify.notify_dir() == notify_dir

    def test_notify_path_is_ask_id_scoped(self, notify_dir):
        assert ask_notify.notify_path(42) == notify_dir / "42.notify"

    def test_notify_path_does_not_precreate_file(self, notify_dir):
        """notify_pathを呼んだだけではファイルは作られない（事前生成しない設計）。"""
        path = ask_notify.notify_path(42)
        assert not path.exists()


class TestWriteNotification:
    def test_creates_dir_and_file_on_first_write(self, notify_dir):
        assert not notify_dir.exists()
        ask_notify.write_notification(7, "answered")
        path = notify_dir / "7.notify"
        assert path.exists()

    def test_appends_json_line_with_ask_id_and_status(self, notify_dir):
        ask_notify.write_notification(7, "answered")
        path = notify_dir / "7.notify"
        line = path.read_text().strip()
        record = json.loads(line)
        assert record["ask_id"] == 7
        assert record["status"] == "answered"
        assert "at" in record

    def test_second_write_appends_not_overwrites(self, notify_dir):
        ask_notify.write_notification(7, "answered")
        ask_notify.write_notification(7, "dismissed")
        path = notify_dir / "7.notify"
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["status"] == "answered"
        assert json.loads(lines[1])["status"] == "dismissed"

    def test_write_failure_is_swallowed_not_raised(self, notify_dir, monkeypatch):
        """ディスク容量等の書き込み失敗は例外を上げず、呼び出し元をブロックしない。"""
        def _boom(ask_id):
            raise OSError("disk full")

        monkeypatch.setattr(ask_notify, "notify_path", _boom)
        # 例外を上げないことだけを確認する（戻り値なし。上げていればテスト自体が落ちる）
        ask_notify.write_notification(7, "answered")


class TestTtlSweep:
    def test_expired_file_is_removed_on_next_write(self, notify_dir):
        notify_dir.mkdir(parents=True)
        old_path = notify_dir / "1.notify"
        old_path.write_text('{"ask_id": 1, "status": "answered"}\n')
        old_time = time.time() - ask_notify._TTL_SECONDS - 60
        os.utime(old_path, (old_time, old_time))

        # 別askへの新規書き込みがスイープのトリガーになる
        ask_notify.write_notification(2, "answered")

        assert not old_path.exists()

    def test_fresh_file_is_not_removed(self, notify_dir):
        notify_dir.mkdir(parents=True)
        fresh_path = notify_dir / "1.notify"
        fresh_path.write_text('{"ask_id": 1, "status": "answered"}\n')

        ask_notify.write_notification(2, "answered")

        assert fresh_path.exists()

    def test_non_notify_files_are_left_alone(self, notify_dir):
        notify_dir.mkdir(parents=True)
        other = notify_dir / "README.txt"
        other.write_text("keep me")
        old_time = time.time() - ask_notify._TTL_SECONDS - 60
        os.utime(other, (old_time, old_time))

        ask_notify.write_notification(2, "answered")

        assert other.exists()
