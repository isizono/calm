"""embedding_server._watchdog の TTL + drain window + force deadline の状態遷移テスト。

A#975 / D#2754 で導入した shutdown policy:
- active → (uptime >= _TTL_SEC) → draining
- draining → (idle >= _DRAIN_IDLE_SEC) → graceful shutdown
- draining → (drain_age >= _DRAIN_DEADLINE_SEC) → force shutdown

watchdog のループ内 time.sleep / time.time を monkey patch して
仮想時間で状態遷移を検証する。
"""
import threading

import pytest

from src.services import embedding_server


class FakeServer:
    """ThreadingHTTPServer の shutdown() だけを記録するスタブ。"""

    def __init__(self):
        self.shutdown_called = False
        self.shutdown_count = 0

    def shutdown(self):
        self.shutdown_called = True
        self.shutdown_count += 1


@pytest.fixture
def reset_state(monkeypatch):
    """モジュールグローバル状態を毎回リセットする。"""
    monkeypatch.setattr(embedding_server, "_state", "active")
    monkeypatch.setattr(embedding_server, "_drain_started_at", None)
    monkeypatch.setattr(embedding_server, "_started_at", 1000.0)
    monkeypatch.setattr(embedding_server, "_last_access_time", 1000.0)
    yield


def _run_watchdog_with_virtual_time(monkeypatch, server, ticks):
    """watchdog を 1 スレッド実行し、ticks のリストを time.time() の戻り値として順番に消費する。

    各 sleep は no-op にし、time.time() 呼び出し時に ticks から先頭を pop して返す。
    リストが尽きたら最後の値を返し続ける（テスト側で十分な ticks を渡す責任）。
    """
    sleep_calls = []
    tick_iter = iter(ticks)
    last = [ticks[-1] if ticks else 0.0]

    def fake_sleep(sec):
        sleep_calls.append(sec)

    def fake_time():
        try:
            v = next(tick_iter)
            last[0] = v
            return v
        except StopIteration:
            return last[0]

    monkeypatch.setattr(embedding_server.time, "sleep", fake_sleep)
    monkeypatch.setattr(embedding_server.time, "time", fake_time)

    embedding_server._watchdog(server)
    return sleep_calls


def test_ttl_transition_to_draining_then_idle_shutdown(monkeypatch, reset_state):
    """TTL 経過で draining に入り、idle >= _DRAIN_IDLE_SEC で shutdown が呼ばれる。"""
    monkeypatch.setattr(embedding_server, "_TTL_SEC", 100)
    monkeypatch.setattr(embedding_server, "_DRAIN_IDLE_SEC", 10)
    monkeypatch.setattr(embedding_server, "_DRAIN_DEADLINE_SEC", 10000)
    monkeypatch.setattr(embedding_server, "_started_at", 1000.0)
    monkeypatch.setattr(embedding_server, "_last_access_time", 1000.0)

    server = FakeServer()

    # tick 1: now=1050（uptime=50, まだ active）
    # tick 2: now=1101（uptime=101 ≥ TTL=100, draining 開始）
    # tick 3: now=1112（idle = 1112-1000 = 112 ≥ DRAIN_IDLE_SEC=10 → shutdown）
    ticks = [1050.0, 1101.0, 1112.0]
    _run_watchdog_with_virtual_time(monkeypatch, server, ticks)

    assert server.shutdown_called is True
    assert embedding_server._state == "draining"
    assert embedding_server._drain_started_at == 1101.0


def test_ttl_transition_then_force_deadline(monkeypatch, reset_state):
    """draining 中に drain_age >= _DRAIN_DEADLINE_SEC なら force shutdown する。

    idle 短い（_last_access_time が常に更新されている状況）ケースを模擬する。
    """
    monkeypatch.setattr(embedding_server, "_TTL_SEC", 100)
    monkeypatch.setattr(embedding_server, "_DRAIN_IDLE_SEC", 1000)  # 大きく
    monkeypatch.setattr(embedding_server, "_DRAIN_DEADLINE_SEC", 50)
    monkeypatch.setattr(embedding_server, "_started_at", 1000.0)
    monkeypatch.setattr(embedding_server, "_last_access_time", 1150.0)  # 近い時刻に常に更新

    server = FakeServer()

    # tick 1: now=1101（uptime=101, draining 開始, _drain_started_at=1101）
    # tick 2: now=1155（idle=1155-1150=5 < 1000、drain_age=1155-1101=54 ≥ 50 → force shutdown）
    ticks = [1101.0, 1155.0]
    _run_watchdog_with_virtual_time(monkeypatch, server, ticks)

    assert server.shutdown_called is True
    assert embedding_server._state == "draining"


def test_active_state_no_shutdown_before_ttl(monkeypatch, reset_state):
    """TTL 未経過なら shutdown が呼ばれず、active のまま維持される。"""
    monkeypatch.setattr(embedding_server, "_TTL_SEC", 10000)
    monkeypatch.setattr(embedding_server, "_DRAIN_IDLE_SEC", 10)
    monkeypatch.setattr(embedding_server, "_DRAIN_DEADLINE_SEC", 100)
    monkeypatch.setattr(embedding_server, "_started_at", 1000.0)
    monkeypatch.setattr(embedding_server, "_last_access_time", 1000.0)

    server = FakeServer()

    # tick 終わりまで TTL に届かない設計にし、ticks 枯渇後は最後の tick を繰り返す。
    # ただし watchdog はループするので、shutdown が呼ばれない限り無限ループする。
    # 別 thread で実行し、shutdown_called が短時間 False のままなら OK と判定する。
    sleep_calls = []

    def fake_sleep(sec):
        sleep_calls.append(sec)
        # 5 回 sleep したら強制的に shutdown して脱出する
        if len(sleep_calls) >= 5:
            server.shutdown()
            raise SystemExit("forced exit for test")

    monkeypatch.setattr(embedding_server.time, "sleep", fake_sleep)

    counter = [0]

    def fake_time():
        counter[0] += 1
        # TTL に届かないようゆっくり進める
        return 1000.0 + counter[0] * 10  # 1010, 1020, ...

    monkeypatch.setattr(embedding_server.time, "time", fake_time)

    try:
        embedding_server._watchdog(server)
    except SystemExit:
        pass

    # state が active のまま維持されていることを確認
    assert embedding_server._state == "active"
    # shutdown はテスト側の強制脱出でのみ呼ばれている
    assert server.shutdown_count == 1


def test_active_keeps_when_uptime_below_ttl_but_drain_idle_would_match(monkeypatch, reset_state):
    """active 状態では idle 条件をチェックしない（draining に入って初めて idle 判定する）。"""
    monkeypatch.setattr(embedding_server, "_TTL_SEC", 10000)
    monkeypatch.setattr(embedding_server, "_DRAIN_IDLE_SEC", 5)
    monkeypatch.setattr(embedding_server, "_DRAIN_DEADLINE_SEC", 100)
    monkeypatch.setattr(embedding_server, "_started_at", 1000.0)
    # 最後アクセスから十分時間経っているように見せる
    monkeypatch.setattr(embedding_server, "_last_access_time", 0.0)

    server = FakeServer()

    sleep_calls = []

    def fake_sleep(sec):
        sleep_calls.append(sec)
        if len(sleep_calls) >= 3:
            # 強制脱出
            server.shutdown()
            raise SystemExit("forced exit for test")

    monkeypatch.setattr(embedding_server.time, "sleep", fake_sleep)

    counter = [0]

    def fake_time():
        counter[0] += 1
        return 1000.0 + counter[0] * 1.0  # TTL=10000 にぜんぜん届かない

    monkeypatch.setattr(embedding_server.time, "time", fake_time)

    try:
        embedding_server._watchdog(server)
    except SystemExit:
        pass

    # active のまま、idle 条件は draining 状態でしか効かない
    assert embedding_server._state == "active"
    # テスト側強制脱出のみ
    assert server.shutdown_count == 1


def test_module_level_defaults_match_spec():
    """現在ロード済みモジュールのデフォルト値が plan.md 通り (3600 / 30 / 1800) であることを確認する。

    注意: このアサーションが検証するのは「import 時の env var 未設定状態で読み込まれた値」だけ。
    env var による上書きが実際に効くことは `test_env_var_overrides_apply_on_reimport` で別途検証する。
    """
    import os as _os
    if any(k in _os.environ for k in (
        "CC_MEMORY_EMBEDDING_TTL_SEC",
        "CC_MEMORY_EMBEDDING_DRAIN_IDLE_SEC",
        "CC_MEMORY_EMBEDDING_DRAIN_DEADLINE_SEC",
    )):
        pytest.skip("env var override active; module-level defaults not observable here")

    assert embedding_server._TTL_SEC == 3600
    assert embedding_server._DRAIN_IDLE_SEC == 30
    assert embedding_server._DRAIN_DEADLINE_SEC == 1800


def test_env_var_overrides_apply_on_reimport(monkeypatch):
    """env var を設定してモジュールを再 import すると、グローバル定数が上書きされることを検証する。

    モジュールトップレベルで env var を読む実装に対する正攻法のテスト。
    importlib.reload で初期化ロジックを再走させ、グローバル値が env var の値に置き換わることを確認。
    """
    import importlib

    monkeypatch.setenv("CC_MEMORY_EMBEDDING_TTL_SEC", "111")
    monkeypatch.setenv("CC_MEMORY_EMBEDDING_DRAIN_IDLE_SEC", "22")
    monkeypatch.setenv("CC_MEMORY_EMBEDDING_DRAIN_DEADLINE_SEC", "333")

    try:
        importlib.reload(embedding_server)
        assert embedding_server._TTL_SEC == 111
        assert embedding_server._DRAIN_IDLE_SEC == 22
        assert embedding_server._DRAIN_DEADLINE_SEC == 333
    finally:
        # 後続テストへ影響しないよう env を剥がしてもう一度 reload
        monkeypatch.delenv("CC_MEMORY_EMBEDDING_TTL_SEC", raising=False)
        monkeypatch.delenv("CC_MEMORY_EMBEDDING_DRAIN_IDLE_SEC", raising=False)
        monkeypatch.delenv("CC_MEMORY_EMBEDDING_DRAIN_DEADLINE_SEC", raising=False)
        importlib.reload(embedding_server)
