"""src/main.py の _setup_server_logging のユニットテスト

launcher が stdout/stderr を DEVNULL でサーバーを起動するため、
RotatingFileHandler によるファイル永続化が正しく機能することを検証する。
"""
import logging

import pytest

from src.main import _setup_server_logging


@pytest.fixture
def _clean_root_handlers():
    """テスト前後でroot loggerのhandlersを退避・復元する。

    _setup_server_logging はグローバルなroot loggerを変更するため、
    他テストへの副作用（handler蓄積・レベル変更）を防ぐ。
    """
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield root
    for h in root.handlers:
        if h not in original_handlers:
            root.removeHandler(h)
            h.close()
    root.setLevel(original_level)


def test_creates_log_directory_and_file(tmp_path, _clean_root_handlers):
    db_path = str(tmp_path / "db" / "discussion.db")

    log_dir = _setup_server_logging(db_path)

    assert log_dir == tmp_path / "db" / "logs"
    assert log_dir.is_dir()


def test_log_message_persisted_to_file(tmp_path, _clean_root_handlers):
    db_path = str(tmp_path / "db" / "discussion.db")

    log_dir = _setup_server_logging(db_path)
    logging.getLogger("test.logger").info("hello from test")

    log_file = log_dir / "server.log"
    assert log_file.exists()
    content = log_file.read_text()
    assert "hello from test" in content
    assert "test.logger" in content


def test_root_logger_level_set_to_info(tmp_path, _clean_root_handlers):
    db_path = str(tmp_path / "db" / "discussion.db")

    _setup_server_logging(db_path)

    assert logging.getLogger().level == logging.INFO


def test_directory_mode_is_owner_only(tmp_path, _clean_root_handlers):
    db_path = str(tmp_path / "db" / "discussion.db")

    log_dir = _setup_server_logging(db_path)

    mode = log_dir.stat().st_mode & 0o777
    assert mode == 0o700
