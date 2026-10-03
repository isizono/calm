"""src/infra/file_ops.py のユニットテスト"""
import os

import pytest

from src.infra import file_ops


def test_replace_retrying_succeeds_on_first_try(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("content")

    file_ops.replace_retrying(src, dst)

    assert dst.read_text() == "content"
    assert not src.exists()


def test_replace_retrying_recovers_after_transient_error(tmp_path, monkeypatch):
    """1回目は共有違反を模した失敗、2回目で成功すれば例外を外に漏らさない"""
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("content")

    real_replace = os.replace
    calls = {"count": 0}

    def flaky_replace(a, b):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError(32, "The process cannot access the file")
        return real_replace(a, b)

    monkeypatch.setattr(file_ops.os, "replace", flaky_replace)
    monkeypatch.setattr(file_ops.time, "sleep", lambda _: None)

    file_ops.replace_retrying(src, dst)

    assert calls["count"] == 2
    assert dst.read_text() == "content"


def test_replace_retrying_raises_after_exhausting_attempts(tmp_path, monkeypatch):
    """全試行が失敗すれば最後の例外をそのまま外に出す"""
    def always_fails(a, b):
        raise OSError(32, "The process cannot access the file")

    monkeypatch.setattr(file_ops.os, "replace", always_fails)
    monkeypatch.setattr(file_ops.time, "sleep", lambda _: None)

    with pytest.raises(OSError):
        file_ops.replace_retrying(tmp_path / "src.txt", tmp_path / "dst.txt")
