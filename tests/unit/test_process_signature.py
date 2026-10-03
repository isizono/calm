"""src/infra/process_signature.py のユニットテスト"""
import os

import psutil

from src.infra import process_signature


def test_returns_signature_for_alive_process():
    """自プロセスの起動時刻は取得でき、2回呼んでも同じ値を返す(PID再利用検知の前提)"""
    first = process_signature.process_start_signature(os.getpid())
    second = process_signature.process_start_signature(os.getpid())

    assert first is not None
    assert first == second


def test_none_for_nonexistent_pid():
    assert process_signature.process_start_signature(999999999) is None


def test_none_when_psutil_raises_error(monkeypatch):
    """権限不足等psutil.Error全般で例外を外に漏らさずNoneにする"""
    def raise_access_denied(pid):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(process_signature.psutil, "Process", raise_access_denied)

    assert process_signature.process_start_signature(1234) is None
