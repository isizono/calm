"""src/services/restart_service.py のユニットテスト

subprocess呼び出し(lsof/ps/kill/Popen)を外部境界としてmonkeypatchし、
プロセス入れ替え判定ロジック・キャッシュ削除の契約を検証する。
"""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src.env_compat import env_restore, env_snapshot
from src.infra import lock_file
from src.services import restart_service


@pytest.fixture(autouse=True)
def _isolate_lock_file(tmp_path, monkeypatch):
    """_stop_mcp_server()がserver.lockの後始末でlock_file.release()を呼びうるため、
    テストごとにロックファイルのパスを一時ディレクトリへ差し替える(このマシンで
    実際に稼働中のサーバーの~/.cc-memory/server.lockを読み書きしないため)。
    """
    lock_dir = tmp_path / ".cc-memory"
    lock_dir.mkdir()
    monkeypatch.setattr(lock_file, "LOCK_DIR", lock_dir)
    monkeypatch.setattr(lock_file, "LOCK_FILE", lock_dir / "server.lock")


@pytest.fixture(autouse=True)
def _default_to_posix_platform(monkeypatch):
    """既定でPOSIX分岐を通す。

    本ファイルの大半のテストはsubprocess呼び出し自体をfakeに差し替えており、
    実行ホストのOSに関わらずfind_listen_pids/kill_pids/popen_detachedの
    POSIX分岐を検証する意図を持つ(Windows分岐はtest名で明示し、個別に
    sys.platform="win32"を上書きする)。この既定が無いと、実機のWindows CI上
    ではsys.platformが本当に"win32"になるため、POSIX分岐を検証するつもりの
    テストが無言でWindows分岐（psutil等、未fakeの実呼び出し）を通ってしまう。
    """
    monkeypatch.setattr(restart_service.sys, "platform", "darwin")


@pytest.fixture(autouse=True)
def _isolate_calm_project_root_env():
    """restart_mcp_server()のenv_set("CALM_PROJECT_ROOT", ...)、os.environ.setdefault
    ("PYTHONUTF8", ...)はos.environを直接書き換えるため、monkeypatch.delenv
    (raising=False)では捕捉されない(対象キーが元々未設定だとundo記録が残らない:
    pytest monkeypatchの仕様)。放置するとrestart_mcp_server()を呼ぶどのテストからも
    これらの環境変数がプロセス全体に残留しうるため、本ファイル全体に適用して
    テスト順依存の非決定性を防ぐ。
    """
    snapshot = env_snapshot("CALM_PROJECT_ROOT")
    python_utf8 = os.environ.get("PYTHONUTF8")
    yield
    env_restore(snapshot)
    if python_utf8 is None:
        os.environ.pop("PYTHONUTF8", None)
    else:
        os.environ["PYTHONUTF8"] = python_utf8


def test_find_listen_pids_parses_lsof_output(monkeypatch):
    captured_cmd = []

    def fake_run(cmd, **kwargs):
        captured_cmd.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="1234\n5678\n", stderr="")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    pids = restart_service.find_listen_pids(52837)

    assert pids == [1234, 5678]
    assert captured_cmd == [["lsof", "-ti", "tcp:52837", "-sTCP:LISTEN"]]


def test_find_listen_pids_empty_when_nothing_listens(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service.find_listen_pids(52837) == []


def test_find_listen_pids_empty_on_timeout(monkeypatch):
    """lsofがハングした場合でも再起動フロー全体を無期限にブロックしない"""
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service.find_listen_pids(52837) == []


def test_find_listen_pids_dedupes_and_sorts(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="5678\n1234\n1234\n", stderr="")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service.find_listen_pids(52837) == [1234, 5678]


def test_find_listen_pids_windows_uses_psutil_not_lsof(monkeypatch):
    """Windowsにはlsofが無いためpsutilのLISTEN接続一覧から該当ポートのpidを拾う"""
    monkeypatch.setattr(restart_service.sys, "platform", "win32")

    def fake_run(cmd, **kwargs):
        raise AssertionError("Windows分岐ではlsofを呼んではいけない")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    conns = [
        SimpleNamespace(pid=111, status=restart_service.psutil.CONN_LISTEN,
                         laddr=SimpleNamespace(port=52837)),
        SimpleNamespace(pid=222, status=restart_service.psutil.CONN_LISTEN,
                         laddr=SimpleNamespace(port=9999)),  # 別ポートは除外
        SimpleNamespace(pid=333, status="ESTABLISHED",
                         laddr=SimpleNamespace(port=52837)),  # LISTEN以外は除外
        SimpleNamespace(pid=None, status=restart_service.psutil.CONN_LISTEN,
                         laddr=SimpleNamespace(port=52837)),  # pid不明(権限不足等)は除外
    ]
    monkeypatch.setattr(restart_service.psutil, "net_connections", lambda kind="inet": conns)

    assert restart_service.find_listen_pids(52837) == [111]


def test_find_listen_pids_windows_empty_on_access_denied(monkeypatch):
    """権限不足で接続一覧が取得できない場合は空リストにする(安全側)"""
    monkeypatch.setattr(restart_service.sys, "platform", "win32")

    def fake_net_connections(kind="inet"):
        raise restart_service.psutil.AccessDenied()

    monkeypatch.setattr(restart_service.psutil, "net_connections", fake_net_connections)

    assert restart_service.find_listen_pids(52837) == []


def test_kill_pids_sends_sigterm_only_when_process_dies_promptly(monkeypatch):
    """SIGTERMだけで終了する場合はSIGKILLへエスカレーションしない"""
    signals_sent = []

    def fake_kill(pid, sig):
        signals_sent.append((pid, sig))

    monkeypatch.setattr(restart_service.os, "kill", fake_kill)
    # 生存確認: SIGTERM後すぐ死んだ想定
    monkeypatch.setattr(restart_service, "is_process_alive", lambda pid: False)

    restart_service.kill_pids([1234])

    assert signals_sent == [(1234, restart_service.signal.SIGTERM)]


@pytest.mark.skipif(sys.platform == "win32", reason="signal.SIGKILLはWindowsに存在しない。Windows版はtest_kill_pids_windows_*で検証する")
def test_kill_pids_escalates_to_sigkill_when_process_survives_sigterm(monkeypatch):
    """SIGTERMを送っても生存し続けるプロセスにはSIGKILLを送る"""
    signals_sent = []

    def fake_kill(pid, sig):
        signals_sent.append((pid, sig))

    monkeypatch.setattr(restart_service.os, "kill", fake_kill)
    monkeypatch.setattr(restart_service, "is_process_alive", lambda pid: True)
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)

    restart_service.kill_pids([1234], escalate_after_sec=0, poll_interval_sec=0)

    assert (1234, restart_service.signal.SIGTERM) in signals_sent
    assert (1234, restart_service.signal.SIGKILL) in signals_sent


def test_kill_pids_windows_terminates_once_via_psutil(monkeypatch):
    """Windowsではsignal.SIGKILLが存在せず、os.kill(pid, SIGTERM)もTerminateProcess
    として即座に終了するだけでSIGTERMの猶予的な意味を持たないため、エスカレーション
    せずpsutilのterminate()を1回送って生存確認で待つだけにする。

    signal.SIGKILL・os.killpg・os.getpgidを削除してから実行することで、
    Windows分岐がこれらを参照しないこと自体を検証する(参照すれば
    AttributeErrorになり、Windows実機と同じ壊れ方を再現できる)。

    is_process_aliveをTrue→True→Falseの順で返すfakeにし、「生きている→死ぬ」の
    遷移を実際に通す。この遷移が無いと、待ちループを丸ごと消しても、ポーリングの
    たびにterminateを送り直しても、このテストは両者を見分けられない。
    """
    monkeypatch.setattr(restart_service.sys, "platform", "win32")
    monkeypatch.delattr(restart_service.signal, "SIGKILL", raising=False)
    monkeypatch.delattr(restart_service.os, "kill", raising=False)
    monkeypatch.delattr(restart_service.os, "killpg", raising=False)
    monkeypatch.delattr(restart_service.os, "getpgid", raising=False)

    terminated = []

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def terminate(self):
            terminated.append(self.pid)

    monkeypatch.setattr(restart_service.psutil, "Process", FakeProcess)

    alive_sequence = iter([True, True, False])
    alive_calls = []

    def fake_is_process_alive(pid):
        alive_calls.append(pid)
        return next(alive_sequence)

    monkeypatch.setattr(restart_service, "is_process_alive", fake_is_process_alive)
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)

    restart_service.kill_pids([4242])

    assert terminated == [4242]
    # alive_sequenceの3要素(True, True, False)を使い切ったことを確かめる。
    # 2回までしか消費しない変異(1回ポーリングしたら生死を問わず諦める、
    # whileをifにする等)は、len(alive_calls) >= 2 のままでは検知できない。
    assert len(alive_calls) == 3


def test_kill_pids_windows_ignores_already_gone_process(monkeypatch):
    monkeypatch.setattr(restart_service.sys, "platform", "win32")
    monkeypatch.delattr(restart_service.os, "kill", raising=False)
    monkeypatch.delattr(restart_service.os, "killpg", raising=False)
    monkeypatch.delattr(restart_service.os, "getpgid", raising=False)

    def fake_process(pid):
        raise restart_service.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(restart_service.psutil, "Process", fake_process)
    monkeypatch.setattr(restart_service, "is_process_alive", lambda pid: False)

    restart_service.kill_pids([4242])  # 例外が出なければOK


def test_is_replaced_true_when_new_pid_unseen_before(monkeypatch):
    monkeypatch.setattr(restart_service, "process_start_signature", lambda pid: "sig")

    assert restart_service._is_replaced({}, [9999]) is True


def test_is_replaced_true_when_signature_changed_for_same_pid(monkeypatch):
    """PID再利用のケース: 同じPID番号でも起動時刻が変わっていれば別プロセスとみなす"""
    monkeypatch.setattr(restart_service, "process_start_signature", lambda pid: "new-sig")

    assert restart_service._is_replaced({1234: "old-sig"}, [1234]) is True


def test_is_replaced_false_when_signature_unchanged(monkeypatch):
    """旧プロセスがkillされず生き残っているケース: 入れ替わっていないと判定する"""
    monkeypatch.setattr(restart_service, "process_start_signature", lambda pid: "same-sig")

    assert restart_service._is_replaced({1234: "same-sig"}, [1234]) is False


def test_restart_mcp_server_success_flow(monkeypatch, tmp_path):
    """旧PID記録 → kill → 新プロセス起動 → 起動時刻検証、の一連の流れを検証する。

    find_listen_pidsの呼び出し回数・順序ではなく、kill完了/新規プロセス起動という
    「状態」に対して一貫した値を返すfakeにする。これにより、実装が途中で
    追加のLISTEN確認を挟むように変わっても、observable contract（最終的な
    RestartResult）が同じである限りテストは壊れない。
    """
    state = {"killed": False, "new_server_started": False}

    def fake_find_listen_pids(port):
        if state["new_server_started"]:
            return [2222]
        if state["killed"]:
            return []
        return [1111]

    def fake_kill_pids(pids):
        assert pids == [1111]
        state["killed"] = True

    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        state["new_server_started"] = True
        return SimpleNamespace(pid=2222)

    monkeypatch.setattr(restart_service, "find_listen_pids", fake_find_listen_pids)
    monkeypatch.setattr(
        restart_service, "process_start_signature",
        lambda pid: {1111: "old-sig", 2222: "new-sig"}.get(pid),
    )
    monkeypatch.setattr(restart_service, "kill_pids", fake_kill_pids)
    monkeypatch.setattr(restart_service, "_resolve_main_repo_root", lambda project_root: project_root)
    monkeypatch.setattr(restart_service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)
    monkeypatch.setattr(restart_service, "LAUNCHER_LOG_PATH", tmp_path / "logs" / "restart_launcher.log")

    result = restart_service.restart_mcp_server(tmp_path, poll_interval_sec=0)

    assert result.ok is True
    assert result.old_pids == [1111]
    assert result.new_pids == [2222]
    assert state["killed"] is True
    assert len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    assert cmd == ["uv", "run", "--directory", str(tmp_path), "python", "-m", "src.launcher"]
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == str(tmp_path)


def test_restart_mcp_server_uses_popen_detached_windows_wiring(monkeypatch, tmp_path):
    """popen_detached経由でWindows用kwargsが渡ること

    popen_detachedを経由せずstart_new_session=Trueで直接起動する実装に戻しても
    気づけない回帰を防ぐため、popen_detachedのWindows分岐(中継プロセスの起動)が
    実際に呼び出されることを確かめる。
    """
    from src.infra import detached_process

    state = {"new_server_started": False}

    def fake_find_listen_pids(port):
        return [2222] if state["new_server_started"] else []

    popen_calls = []

    class FakeRelay:
        def __init__(self, cmd, **kwargs):
            popen_calls.append(kwargs)
            state["new_server_started"] = True
            self.returncode = 0

        def communicate(self, input=None, timeout=None):
            return b"2222\n", b""

    monkeypatch.setattr(restart_service, "find_listen_pids", fake_find_listen_pids)
    monkeypatch.setattr(restart_service, "process_start_signature", lambda pid: "sig")
    monkeypatch.setattr(restart_service, "kill_pids", lambda pids: None)
    monkeypatch.setattr(restart_service, "_resolve_main_repo_root", lambda project_root: project_root)
    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    monkeypatch.setattr(detached_process.psutil, "Process", lambda pid: SimpleNamespace(pid=pid))
    monkeypatch.setattr(restart_service.subprocess, "Popen", FakeRelay)
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)
    monkeypatch.setattr(restart_service, "LAUNCHER_LOG_PATH", tmp_path / "logs" / "restart_launcher.log")

    result = restart_service.restart_mcp_server(tmp_path, poll_interval_sec=0)

    assert result.ok is True
    assert result.new_pids == [2222]
    assert len(popen_calls) == 1
    kwargs = popen_calls[0]
    assert kwargs["creationflags"] == (
        detached_process._CREATE_NEW_PROCESS_GROUP
        | detached_process._CREATE_NO_WINDOW
        | detached_process._CREATE_BREAKAWAY_FROM_JOB
    )
    assert kwargs["stdin"] == subprocess.PIPE
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.PIPE


def test_restart_mcp_server_skips_kill_when_nothing_was_listening(monkeypatch, tmp_path):
    """サーバーが元から起動していない場合はkillを呼ばずそのまま起動する"""
    state = {"new_server_started": False}

    def fake_find_listen_pids(port):
        return [2222] if state["new_server_started"] else []

    monkeypatch.setattr(restart_service, "find_listen_pids", fake_find_listen_pids)
    monkeypatch.setattr(restart_service, "process_start_signature", lambda pid: "sig")

    killed = []
    monkeypatch.setattr(restart_service, "kill_pids", lambda pids: killed.extend(pids))

    def fake_popen(cmd, **kwargs):
        state["new_server_started"] = True
        return SimpleNamespace(pid=2222)

    monkeypatch.setattr(restart_service, "_resolve_main_repo_root", lambda project_root: project_root)
    monkeypatch.setattr(restart_service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)
    monkeypatch.setattr(restart_service, "LAUNCHER_LOG_PATH", tmp_path / "logs" / "restart_launcher.log")

    result = restart_service.restart_mcp_server(tmp_path, poll_interval_sec=0)

    assert result.ok is True
    assert result.old_pids == []
    assert killed == []


@pytest.mark.skipif(sys.platform == "win32", reason="signal.SIGKILLはWindowsに存在しない。Windows版のkill_pidsにはエスカレーションの概念自体が無い")
def test_restart_mcp_server_replaces_old_process_that_ignores_sigterm(monkeypatch, tmp_path):
    """旧プロセスがSIGTERMを無視してもkill_pidsのSIGKILLエスカレーションで
    kill_wait_sec以内に確実に片付き、新規プロセスへ入れ替わることを検証する。

    kill_pidsはmockせず実装をそのまま呼び出す。エスカレーション自体が
    無かった旧実装では、このシナリオはold_pidsがkill_wait_sec(既定10秒)
    経過後も消えずに残り、新規プロセスがポートbindに失敗して
    start_timeout_secでの汎用タイムアウトに陥っていた。
    """
    clock = {"now": 0.0}
    monkeypatch.setattr(restart_service.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(restart_service.time, "sleep", lambda sec: clock.__setitem__("now", clock["now"] + sec))

    process_alive = {1111: True}

    def fake_os_kill(pid, sig):
        if sig == 0:
            if not process_alive.get(pid, False):
                raise ProcessLookupError
            return  # 生存確認: SIGTERMを送っても死なない想定
        if sig == restart_service.signal.SIGKILL:
            process_alive[pid] = False
        # SIGTERMは無視され続ける(何もしない)

    monkeypatch.setattr(restart_service.os, "kill", fake_os_kill)
    # kill_pidsの生存判定はis_process_alive()(psutil)を見る。Linux版psutilは
    # os.kill(pid,0)に加えて/proc/{pid}/statusの実在確認を行うため、存在しない
    # 偽PIDに対してos.kill差し替えだけでは「生存中」を偽装できない。生存判定
    # そのものをprocess_aliveと同期させる。
    monkeypatch.setattr(restart_service, "is_process_alive", lambda pid: process_alive.get(pid, False))

    new_server_started = {"flag": False}

    def fake_find_listen_pids(port):
        # 旧プロセスが生きている限りポートは旧PIDが握り続ける
        # (新規プロセスはbindに失敗して観測されない)。escalationが効かず
        # 旧プロセスが生存し続けた場合、この分岐によりis_replaced判定は
        # 常にFalseのまま推移し、start_timeout_secでのタイムアウトを再現する。
        if process_alive[1111]:
            return [1111]
        return [2222] if new_server_started["flag"] else []

    monkeypatch.setattr(restart_service, "find_listen_pids", fake_find_listen_pids)
    monkeypatch.setattr(
        restart_service, "process_start_signature",
        lambda pid: {1111: "old-sig", 2222: "new-sig"}.get(pid),
    )

    def fake_popen(cmd, **kwargs):
        new_server_started["flag"] = True
        return SimpleNamespace(pid=2222)

    monkeypatch.setattr(restart_service, "_resolve_main_repo_root", lambda project_root: project_root)
    monkeypatch.setattr(restart_service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(restart_service, "LAUNCHER_LOG_PATH", tmp_path / "logs" / "restart_launcher.log")

    result = restart_service.restart_mcp_server(tmp_path)

    assert result.ok is True
    assert result.old_pids == [1111]
    assert result.new_pids == [2222]
    assert process_alive[1111] is False


@pytest.mark.skipif(sys.platform == "win32", reason="os.killpg/os.getpgidはWindowsに存在しない。Windows版はtest_kill_process_group_windows_*で検証する")
def test_restart_mcp_server_proceeds_to_start_new_process_even_if_old_process_never_dies(monkeypatch, tmp_path):
    """SIGKILLを送っても消えない旧プロセス(D state等で応答しないケース)が
    kill_wait_sec以内に片付かない場合、現状の実装はエスカレーションや
    早期失敗を挟まずそのまま新規プロセス起動に進む。この既知の振る舞いを
    固定する(ソフトウェア側の再試行では解決できないOS側の異常なので、
    software側にできるのは早期に失敗を返すことだけだが、現状はそれもしない)。
    """
    clock = {"now": 0.0}
    monkeypatch.setattr(restart_service.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(restart_service.time, "sleep", lambda sec: clock.__setitem__("now", clock["now"] + sec))

    monkeypatch.setattr(restart_service.os, "kill", lambda pid, sig: None)  # 常に成功=常に生存
    monkeypatch.setattr(restart_service, "find_listen_pids", lambda port: [1111])
    monkeypatch.setattr(restart_service, "process_start_signature", lambda pid: "old-sig")
    monkeypatch.setattr(restart_service, "_resolve_main_repo_root", lambda project_root: project_root)
    monkeypatch.setattr(restart_service, "LAUNCHER_LOG_PATH", tmp_path / "logs" / "restart_launcher.log")

    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return SimpleNamespace(pid=9999)

    monkeypatch.setattr(restart_service.subprocess, "Popen", fake_popen)

    killpg_calls = []
    monkeypatch.setattr(restart_service.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(restart_service.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    result = restart_service.restart_mcp_server(
        tmp_path, start_timeout_sec=1, poll_interval_sec=0.5, kill_wait_sec=1,
    )

    assert len(popen_calls) == 1  # kill_wait_sec超過後も新規プロセス起動には進んでしまう
    assert result.ok is False
    assert result.old_pids == [1111]
    assert "did not come up on port 52837" in result.detail
    assert killpg_calls == [(9999, restart_service.signal.SIGKILL)]  # タイムアウト後は子プロセスグループを後始末する


@pytest.mark.skipif(sys.platform == "win32", reason="os.killpg/os.getpgidはWindowsに存在しない。Windows版はtest_kill_process_group_windows_*で検証する")
def test_restart_mcp_server_times_out_when_server_never_comes_up(monkeypatch, tmp_path):
    monkeypatch.setattr(restart_service, "find_listen_pids", lambda port: [])
    monkeypatch.setattr(restart_service, "kill_pids", lambda pids: None)
    monkeypatch.setattr(restart_service, "_resolve_main_repo_root", lambda project_root: project_root)
    monkeypatch.setattr(restart_service, "LAUNCHER_LOG_PATH", tmp_path / "logs" / "restart_launcher.log")
    monkeypatch.setattr(restart_service.subprocess, "Popen", lambda cmd, **kwargs: SimpleNamespace(pid=4321))
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)

    killpg_calls = []
    monkeypatch.setattr(restart_service.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(restart_service.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    result = restart_service.restart_mcp_server(
        tmp_path, start_timeout_sec=0, poll_interval_sec=0, kill_wait_sec=0,
    )

    assert result.ok is False
    assert result.new_pids == []
    assert "did not come up on port 52837" in result.detail
    assert killpg_calls == [(4321, restart_service.signal.SIGKILL)]


@pytest.mark.skipif(sys.platform == "win32", reason="os.killpg/os.getpgidはWindowsに存在しない。Windows版はtest_kill_process_group_windows_*で検証する")
def test_restart_mcp_server_ignores_process_lookup_error_when_killing_process_group(monkeypatch, tmp_path):
    """killpgが対象プロセスの消滅を示すProcessLookupErrorを送出しても後始末全体は失敗にしない"""
    monkeypatch.setattr(restart_service, "find_listen_pids", lambda port: [])
    monkeypatch.setattr(restart_service, "kill_pids", lambda pids: None)
    monkeypatch.setattr(restart_service, "_resolve_main_repo_root", lambda project_root: project_root)
    monkeypatch.setattr(restart_service, "LAUNCHER_LOG_PATH", tmp_path / "logs" / "restart_launcher.log")
    monkeypatch.setattr(restart_service.subprocess, "Popen", lambda cmd, **kwargs: SimpleNamespace(pid=4321))
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)

    def fake_killpg(pgid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(restart_service.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(restart_service.os, "killpg", fake_killpg)

    result = restart_service.restart_mcp_server(
        tmp_path, start_timeout_sec=0, poll_interval_sec=0, kill_wait_sec=0,
    )

    assert result.ok is False
    assert "did not come up on port 52837" in result.detail


def test_kill_process_group_windows_terminates_direct_child_only(monkeypatch):
    """Windowsにはos.killpg/os.getpgid相当が無いため、psutilで直下の子プロセス
    (uv run経由のvenvリダイレクタ等)まで含めてterminateする。

    孫プロセス(例: launcher.py自身が切り離して起動したHTTPサーバー)は対象外にする。
    WindowsのppidはCREATE_NEW_PROCESS_GROUPの影響を受けず起動元を指したままなので、
    children(recursive=True)のまま辿ると切り離したはずのサーバーまで終了させてしまう
    (孫のFakeProc(3)がterminateされないことで、recursive=Falseになっていることを
    間接的に確認する)。
    """
    monkeypatch.setattr(restart_service.sys, "platform", "win32")
    monkeypatch.delattr(restart_service.os, "killpg", raising=False)
    monkeypatch.delattr(restart_service.os, "getpgid", raising=False)

    terminated = []

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def terminate(self):
            terminated.append(self.pid)

    class FakeParent(FakeProc):
        def children(self, recursive=True):
            # 孫プロセス(切り離されたサーバー相当、pid=3)はrecursive=Trueのときのみ
            # 含まれる。recursive=Falseなら直下の子(venvリダイレクタ相当、pid=2)だけ。
            if recursive:
                return [FakeProc(2), FakeProc(3)]
            return [FakeProc(2)]

    monkeypatch.setattr(restart_service.psutil, "Process", lambda pid: FakeParent(pid))

    restart_service._kill_process_group(SimpleNamespace(pid=1))

    assert terminated == [1, 2]


def test_kill_process_group_windows_ignores_already_gone_process(monkeypatch):
    monkeypatch.setattr(restart_service.sys, "platform", "win32")
    monkeypatch.delattr(restart_service.os, "killpg", raising=False)
    monkeypatch.delattr(restart_service.os, "getpgid", raising=False)

    def fake_process(pid):
        raise restart_service.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(restart_service.psutil, "Process", fake_process)

    restart_service._kill_process_group(SimpleNamespace(pid=1))  # 例外が出なければOK


def test_restart_mcp_server_writes_launcher_output_to_log_file(monkeypatch, tmp_path):
    """launcherのstdout/stderrをDEVNULLではなくログファイルへまとめる"""
    log_path = tmp_path / "logs" / "restart_launcher.log"
    monkeypatch.setattr(restart_service, "LAUNCHER_LOG_PATH", log_path)

    state = {"new_server_started": False}

    def fake_find_listen_pids(port):
        return [2222] if state["new_server_started"] else []

    monkeypatch.setattr(restart_service, "find_listen_pids", fake_find_listen_pids)
    monkeypatch.setattr(restart_service, "process_start_signature", lambda pid: "sig")
    monkeypatch.setattr(restart_service, "_resolve_main_repo_root", lambda project_root: project_root)

    popen_kwargs = {}

    def fake_popen(cmd, **kwargs):
        popen_kwargs.update(kwargs)
        state["new_server_started"] = True
        return SimpleNamespace(pid=1)

    monkeypatch.setattr(restart_service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)

    restart_service.restart_mcp_server(tmp_path, poll_interval_sec=0)

    assert log_path.parent.is_dir()
    assert popen_kwargs["stdout"].name == str(log_path)
    assert popen_kwargs["stderr"] is popen_kwargs["stdout"]


class TestRestartMcpServerPropagatesCalmProjectRoot:
    """restart_mcp_server: 新規launcherプロセスへのCALM_PROJECT_ROOT伝播

    Popenはenv未指定でos.environを継承するため、restart_mcp_server自身が
    プロセス環境変数を書き換えているかをos.environで直接検証する
    (残留防止は_isolate_calm_project_root_env、モジュールレベルのautouse fixture)。

    _resolve_main_repo_root()はデフォルトでproject_rootをそのまま返す恒等関数に
    差し替える。本クラスの関心は「未設定時にenv_setで書き込むか/既存値を尊重するか」
    という配線であり、_resolve_main_repo_root自身のgit解決ロジックは
    TestResolveMainRepoRootで個別に検証する。恒等関数に差し替えないと、
    tmp_pathはgitリポジトリでないため実装は正しく動作するものの、実際には
    「git rev-parse」の実行を試みる形で標準ライブラリのsubprocess run関数を
    呼び出すことになり、このクラスと同様にPopenをfakeへ差し替えているテストでは
    その内部実装がPopen呼び出しへ委譲する構造ゆえfakeを踏んでしまい壊れる
    (Popenのfakeが返すSimpleNamespaceはcontext managerではないため、標準
    ライブラリ内部がwith文でそれを開こうとしてエラーになる)。
    """

    def _run_restart(self, monkeypatch, tmp_path, *, resolve_main_repo_root=None):
        state = {"new_server_started": False}

        def fake_find_listen_pids(port):
            return [2222] if state["new_server_started"] else []

        def fake_popen(cmd, **kwargs):
            state["new_server_started"] = True
            return SimpleNamespace(pid=2222)

        monkeypatch.setattr(restart_service, "find_listen_pids", fake_find_listen_pids)
        monkeypatch.setattr(restart_service, "process_start_signature", lambda pid: "sig")
        monkeypatch.setattr(
            restart_service, "_resolve_main_repo_root",
            resolve_main_repo_root or (lambda project_root: project_root),
        )
        monkeypatch.setattr(restart_service.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)
        monkeypatch.setattr(
            restart_service, "LAUNCHER_LOG_PATH", tmp_path / "logs" / "restart_launcher.log"
        )
        return restart_service.restart_mcp_server(tmp_path, poll_interval_sec=0)

    def test_sets_calm_project_root_when_unset(self, monkeypatch, tmp_path):
        """CALM_PROJECT_ROOT(新旧名とも)が未設定なら、_resolve_main_repo_root()の
        戻り値で設定する

        プラグインキャッシュ配置ではlauncher起動時のCLAUDE_PLUGIN_ROOT頼みの
        自動設定が新規プロセスに伝播している保証がないため、この再起動経路では
        明示的に設定して子プロセスチェーン全体に伝播させる。
        """
        monkeypatch.delenv("CALM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CCM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CC_MEMORY_PROJECT_ROOT", raising=False)

        self._run_restart(monkeypatch, tmp_path)

        assert os.environ["CALM_PROJECT_ROOT"] == str(tmp_path)

    def test_does_not_override_existing_calm_project_root(self, monkeypatch, tmp_path):
        """CALM_PROJECT_ROOTが既に設定済みなら、project_rootの値で上書きしない"""
        monkeypatch.setenv("CALM_PROJECT_ROOT", "/explicit/root")

        self._run_restart(monkeypatch, tmp_path)

        assert os.environ["CALM_PROJECT_ROOT"] == "/explicit/root"

    def test_does_not_override_when_only_legacy_name_set(self, monkeypatch, tmp_path):
        """旧名(CC_MEMORY_PROJECT_ROOT)のみが設定済みの場合も、新名へ書き込まない

        env_get/env_setの新旧名解決ロジック自体は別途テスト済みだが、
        「新旧名含む」というこの修正自体の主張に対応するassertを本クラスにも置く。
        """
        monkeypatch.delenv("CALM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CCM_PROJECT_ROOT", raising=False)
        monkeypatch.setenv("CC_MEMORY_PROJECT_ROOT", "/legacy/root")

        self._run_restart(monkeypatch, tmp_path)

        assert "CALM_PROJECT_ROOT" not in os.environ
        assert os.environ["CC_MEMORY_PROJECT_ROOT"] == "/legacy/root"

    def test_sets_calm_project_root_to_resolved_main_repo_root_not_worktree_path(
        self, monkeypatch, tmp_path,
    ):
        """project_rootがworktreeのようにmain repoルートと異なる場合、
        _resolve_main_repo_root()が解決した値を設定する(project_rootそのものではない)

        restart_mcp_server()自身が誤ったパスを検証なしに設定しないことの結線を
        確認する(git-common-dir解決ロジック自体はTestResolveMainRepoRootの担当)。
        """
        monkeypatch.delenv("CALM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CCM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CC_MEMORY_PROJECT_ROOT", raising=False)

        worktree_root = tmp_path / "worktree"
        worktree_root.mkdir()
        main_repo_root = tmp_path / "main-repo"

        self._run_restart(
            monkeypatch, worktree_root,
            resolve_main_repo_root=lambda project_root: main_repo_root,
        )

        assert os.environ["CALM_PROJECT_ROOT"] == str(main_repo_root)


def _run_restart_minimal(monkeypatch, tmp_path):
    """restart_mcp_server()を、プロセス入れ替え判定に無関係な箇所だけfakeにして実行する。

    find_listen_pidsが常に[]を返すためサーバーは起動確認できず、timeoutで
    後始末の_kill_process_groupへ進む。_default_to_posix_platformによりPOSIX
    分岐(os.killpg/os.getpgid)を通るが、これらはWindowsのos/signalモジュール
    には存在しないため、raising=Falseで外部境界(os.killpg/os.getpgid/
    signal.SIGKILL)をfakeにする。
    """
    monkeypatch.setattr(restart_service, "find_listen_pids", lambda port: [])
    monkeypatch.setattr(restart_service, "kill_pids", lambda pids: None)
    monkeypatch.setattr(restart_service, "_resolve_main_repo_root", lambda project_root: project_root)
    monkeypatch.setattr(restart_service.subprocess, "Popen", lambda cmd, **kwargs: SimpleNamespace(pid=4321))
    monkeypatch.setattr(restart_service, "LAUNCHER_LOG_PATH", tmp_path / "logs" / "restart_launcher.log")
    monkeypatch.setattr(restart_service.os, "killpg", lambda pgid, sig: None, raising=False)
    monkeypatch.setattr(restart_service.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(restart_service.signal, "SIGKILL", 9, raising=False)


def test_restart_mcp_server_sets_python_utf8_when_unset(monkeypatch, tmp_path):
    """.mcp.jsonのcalm.env経由ではないこの再起動フローでも、新規launcherプロセスの
    環境にPYTHONUTF8=1が伝播するよう、未設定ならos.environへ明示的に設定する。
    """
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    _run_restart_minimal(monkeypatch, tmp_path)

    restart_service.restart_mcp_server(tmp_path, poll_interval_sec=0, start_timeout_sec=0)

    assert os.environ["PYTHONUTF8"] == "1"


def test_restart_mcp_server_does_not_override_existing_python_utf8(monkeypatch, tmp_path):
    """PYTHONUTF8が既に設定済みなら上書きしない(明示的に無効化している利用者を尊重する)"""
    monkeypatch.setenv("PYTHONUTF8", "0")
    _run_restart_minimal(monkeypatch, tmp_path)

    restart_service.restart_mcp_server(tmp_path, poll_interval_sec=0, start_timeout_sec=0)

    assert os.environ["PYTHONUTF8"] == "0"


class TestStopMcpServerLockCleanup:
    """_stop_mcp_server(): 停止確認後のserver.lock後始末の契約を検証する。

    Windowsのterminate(TerminateProcess相当)はrelease()のfinally節を経由しない
    ため、停止確認後もserver.lockが残り続ける(POSIXでもSIGKILL強制終了の場合は
    同様)。殺したpidと記録pidが一致し、かつ既に死んでいる場合だけ消す。
    """

    def _run_stop(self, monkeypatch, *, recorded_pid, process_alive):
        calls = {"n": 0}

        def fake_find_listen_pids(port):
            calls["n"] += 1
            return [1111] if calls["n"] == 1 else []

        monkeypatch.setattr(restart_service, "find_listen_pids", fake_find_listen_pids)
        monkeypatch.setattr(restart_service, "process_start_signature", lambda pid: "sig")
        monkeypatch.setattr(restart_service, "kill_pids", lambda pids: None)
        monkeypatch.setattr(restart_service, "is_process_alive", lambda pid: process_alive)
        lock_file.LOCK_FILE.write_text(
            json.dumps({"pid": recorded_pid, "port": 52837}), encoding="utf-8",
        )
        return restart_service._stop_mcp_server(kill_wait_sec=0, poll_interval_sec=0)

    def test_clears_lock_when_killed_pid_matches_and_dead(self, monkeypatch):
        old_pids, _ = self._run_stop(monkeypatch, recorded_pid=1111, process_alive=False)

        assert old_pids == [1111]
        assert lock_file.read() is None

    def test_keeps_lock_when_recorded_pid_differs(self, monkeypatch):
        """停止後に別プロセスが新たにロックを取り直していた場合は消さない
        (respawnレース: 別セッションのlauncherが先に新サーバーを起動した場合)"""
        self._run_stop(monkeypatch, recorded_pid=9999, process_alive=False)

        assert lock_file.read() == {"pid": 9999, "port": 52837, "start_time": None}

    def test_keeps_lock_when_pid_still_alive(self, monkeypatch):
        """kill_wait_sec以内に死ななかった場合、生存中のプロセスのlockを誤って消さない"""
        self._run_stop(monkeypatch, recorded_pid=1111, process_alive=True)

        assert lock_file.read() is not None


def test_restart_all_windows_stops_before_sync(monkeypatch, tmp_path):
    """Windowsでは稼働中のサーバーが`.venv`配下のファイルを開いたままにするため、
    POSIXと逆にサーバーを先に止めてからsyncする。
    """
    monkeypatch.setattr(restart_service.sys, "platform", "win32")
    call_order = []

    def fake_stop_mcp_server(kill_wait_sec, poll_interval_sec):
        call_order.append("stop")
        return [1111], {1111: "old-sig"}

    def fake_sync_dependencies(project_root):
        call_order.append("sync")
        return restart_service.SyncResult(True, 0.1, "synced")

    def fake_clean_caches(project_root):
        call_order.append("clean_caches")
        return {"removed_pycache_dirs": []}

    def fake_start_mcp_server(project_root, old_pids, old_signatures, *, start_timeout_sec, poll_interval_sec):
        call_order.append("start")
        return restart_service.RestartResult(True, old_pids, [2222], "restarted")

    monkeypatch.setattr(restart_service, "_stop_mcp_server", fake_stop_mcp_server)
    monkeypatch.setattr(restart_service, "sync_dependencies", fake_sync_dependencies)
    monkeypatch.setattr(restart_service, "clean_caches", fake_clean_caches)
    monkeypatch.setattr(restart_service, "_start_mcp_server", fake_start_mcp_server)
    monkeypatch.setattr(restart_service, "stop_embedding_server", lambda: [])
    monkeypatch.setattr(
        restart_service, "prune_orphaned_plugin_versions",
        lambda project_root: {"removed": [], "skipped": []},
    )

    result = restart_service.restart_all(tmp_path)

    assert call_order == ["stop", "sync", "clean_caches", "start"]
    assert result["mcp_server"]["ok"] is True
    assert result["mcp_server"]["old_pids"] == [1111]
    assert result["mcp_server"]["new_pids"] == [2222]


class TestGetStatus:
    """get_status(): 副作用なしでMCP/embeddingサーバーの稼働状況を返す契約を検証する。"""

    def test_reports_running_server_with_started_at_matching_health_format(self, monkeypatch):
        """started_atは/healthエンドポイントと同じISO8601(UTC)形式にする

        (process_start_signature()が返す不透明な値は等価比較専用で、
        人間・LLMが時刻として読み比べる用途には使わない)。
        """
        def fake_find_listen_pids(port):
            return {restart_service.MCP_PORT: [111], restart_service.EMBEDDING_PORT: []}[port]

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

            def create_time(self):
                return 1735689600.0  # 2025-01-01T00:00:00+00:00

        monkeypatch.setattr(restart_service, "find_listen_pids", fake_find_listen_pids)
        monkeypatch.setattr(restart_service.psutil, "Process", FakeProcess)

        status = restart_service.get_status()

        assert status["mcp_server"] == {
            "port": restart_service.MCP_PORT, "pids": [111], "running": True,
            "started_at": "2025-01-01T00:00:00+00:00",
        }
        assert status["embedding_server"] == {
            "port": restart_service.EMBEDDING_PORT, "pids": [], "running": False, "started_at": None,
        }

    def test_started_at_none_when_process_vanishes_before_lookup(self, monkeypatch):
        """find_listen_pids()とcreate_time()取得の間にプロセスが消えるTOCTOUレースでも
        例外を出さずNoneにする。"""
        monkeypatch.setattr(restart_service, "find_listen_pids", lambda port: [111])

        def fake_process(pid):
            raise restart_service.psutil.NoSuchProcess(pid)

        monkeypatch.setattr(restart_service.psutil, "Process", fake_process)

        status = restart_service.get_status()

        assert status["mcp_server"]["started_at"] is None


class TestStopAll:
    """stop_all(): 再起動せず停止だけを行う契約を検証する。"""

    def test_stops_mcp_only_by_default(self, monkeypatch):
        monkeypatch.setattr(restart_service, "_stop_mcp_server", lambda kw, pi: ([1111], {1111: "sig"}))
        stop_embedding_calls = []
        monkeypatch.setattr(
            restart_service, "stop_embedding_server",
            lambda: stop_embedding_calls.append(1) or [9999],
        )

        result = restart_service.stop_all()

        assert result == {"mcp_server": {"stopped_pids": [1111]}, "embedding_server": {"stopped_pids": []}}
        assert stop_embedding_calls == []

    def test_stops_embedding_when_requested(self, monkeypatch):
        monkeypatch.setattr(restart_service, "_stop_mcp_server", lambda kw, pi: ([1111], {1111: "sig"}))
        monkeypatch.setattr(restart_service, "stop_embedding_server", lambda: [9999])

        result = restart_service.stop_all(stop_embedding=True)

        assert result == {"mcp_server": {"stopped_pids": [1111]}, "embedding_server": {"stopped_pids": [9999]}}


def test_stop_embedding_server_kills_found_pids(monkeypatch):
    monkeypatch.setattr(
        restart_service, "find_listen_pids",
        lambda port: [3333] if port == restart_service.EMBEDDING_PORT else [],
    )
    killed = []
    monkeypatch.setattr(restart_service, "kill_pids", lambda pids: killed.extend(pids))

    result = restart_service.stop_embedding_server()

    assert result == [3333]
    assert killed == [3333]


def test_stop_embedding_server_noop_when_not_running(monkeypatch):
    monkeypatch.setattr(restart_service, "find_listen_pids", lambda port: [])
    killed = []
    monkeypatch.setattr(restart_service, "kill_pids", lambda pids: killed.extend(pids))

    result = restart_service.stop_embedding_server()

    assert result == []
    assert killed == []


def test_clean_caches_removes_plugin_cache_and_pycache(tmp_path):
    """__pycache__ディレクトリを再帰的に削除する。

    プラグインキャッシュ削除ロジックは、restart_serviceが自身の実行基盤
    (プラグインのコード・venv)を削除しうる危険な機能だったため撤去済み。
    """
    project_root = tmp_path / "project"
    pycache = project_root / "src" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "foo.pyc").write_text("x")

    result = restart_service.clean_caches(project_root)

    assert result == {"removed_pycache_dirs": [str(pycache)]}
    assert not pycache.exists()


def test_clean_caches_handles_missing_plugin_cache_dir(tmp_path):
    """__pycache__が1つも無いプロジェクトルートでもエラーにならない。"""
    project_root = tmp_path / "project"
    project_root.mkdir()

    result = restart_service.clean_caches(project_root)

    assert result == {"removed_pycache_dirs": []}


def test_clean_caches_skips_venv_pycache(tmp_path):
    """.venv配下の__pycache__は削除対象から除外する。

    直後に起動する新規サーバーが依存パッケージを全て再コンパイルする
    事態を避けるため。
    """
    project_root = tmp_path / "project"
    venv_pycache = project_root / ".venv" / "lib" / "site-packages" / "foo" / "__pycache__"
    venv_pycache.mkdir(parents=True)
    (venv_pycache / "foo.pyc").write_text("x")

    src_pycache = project_root / "src" / "__pycache__"
    src_pycache.mkdir(parents=True)
    (src_pycache / "bar.pyc").write_text("x")

    result = restart_service.clean_caches(project_root)

    assert result == {"removed_pycache_dirs": [str(src_pycache)]}
    assert venv_pycache.exists()
    assert not src_pycache.exists()


def test_plugin_cache_dir_removed_from_module():
    """再起動スクリプトが自身の実行基盤を削除しうる機能は撤去済み。"""
    assert not hasattr(restart_service, "PLUGIN_CACHE_DIR")


def test_has_open_file_handles_true_when_lsof_reports_a_match(monkeypatch, tmp_path):
    captured_cmd = []

    def fake_run(cmd, **kwargs):
        captured_cmd.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Python 123 user cwd DIR ...\n", stderr="")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service._has_open_file_handles(tmp_path) is True
    assert captured_cmd == [["lsof", "+D", str(tmp_path)]]


def test_has_open_file_handles_false_when_lsof_reports_nothing(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service._has_open_file_handles(tmp_path) is False


def test_has_open_file_handles_true_on_timeout(monkeypatch, tmp_path):
    """find_listen_pids()と逆向きの安全側判定: 判定不能時は「使用中」として扱い、削除させない"""
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service._has_open_file_handles(tmp_path) is True


def test_has_open_file_handles_true_when_lsof_missing(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("lsof not found")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service._has_open_file_handles(tmp_path) is True


def _no_open_handles(monkeypatch):
    monkeypatch.setattr(
        restart_service, "_has_open_file_handles", lambda path: False,
    )


class TestPruneOrphanedPluginVersions:
    """プラグインキャッシュの旧バージョンディレクトリを兄弟から掃除する契約を検証する。"""

    def test_removes_orphaned_sibling_without_open_handles(self, monkeypatch, tmp_path):
        versions_root = tmp_path / "calm"
        current = versions_root / "be3a99a"
        current.mkdir(parents=True)
        orphaned = versions_root / "1.0.0"
        orphaned.mkdir()
        (orphaned / ".orphaned_at").write_text("1789054301708")
        _no_open_handles(monkeypatch)

        result = restart_service.prune_orphaned_plugin_versions(current)

        assert result == {"removed": [str(orphaned)], "skipped": []}
        assert not orphaned.exists()
        assert current.exists()

    def test_never_removes_current_version_dir_even_if_marked(self, monkeypatch, tmp_path):
        """現在使用中のディレクトリは、たとえマーカーが付いていても兄弟走査の対象にせず、
        識別子の一致だけで無条件に除外する。"""
        versions_root = tmp_path / "calm"
        current = versions_root / "be3a99a"
        current.mkdir(parents=True)
        (current / ".orphaned_at").write_text("1789054301708")
        _no_open_handles(monkeypatch)

        result = restart_service.prune_orphaned_plugin_versions(current)

        assert result == {"removed": [], "skipped": []}
        assert current.exists()

    def test_skips_sibling_without_orphaned_marker(self, monkeypatch, tmp_path):
        """Claude Code自身がまだ現行と判断しているディレクトリ(マーカー無し)は削除しない"""
        versions_root = tmp_path / "calm"
        current = versions_root / "be3a99a"
        current.mkdir(parents=True)
        other = versions_root / "a3c8d3a"
        other.mkdir()
        _no_open_handles(monkeypatch)

        result = restart_service.prune_orphaned_plugin_versions(current)

        assert result == {
            "removed": [],
            "skipped": [{"path": str(other), "reason": "not marked orphaned"}],
        }
        assert other.exists()

    def test_skips_orphaned_sibling_with_open_file_handles(self, monkeypatch, tmp_path):
        """マーカーがあっても、旧バージョンからまだ接続中のプロセス(他セッションのlauncher等)
        が疑われる場合は削除しない"""
        versions_root = tmp_path / "calm"
        current = versions_root / "be3a99a"
        current.mkdir(parents=True)
        orphaned = versions_root / "1.0.0"
        orphaned.mkdir()
        (orphaned / ".orphaned_at").write_text("1789054301708")
        monkeypatch.setattr(restart_service, "_has_open_file_handles", lambda path: True)

        result = restart_service.prune_orphaned_plugin_versions(current)

        assert result == {
            "removed": [],
            "skipped": [{"path": str(orphaned), "reason": "open file handles"}],
        }
        assert orphaned.exists()

    def test_does_nothing_when_project_root_is_a_git_checkout(self, monkeypatch, tmp_path):
        """project_rootがgitリポジトリ(dev worktree)の場合、兄弟ディレクトリは無関係な
        作業ディレクトリの並びでありうるため一切走査しない。"""
        workspace = tmp_path / "workspace"
        current = workspace / "calm"
        (current / ".git").mkdir(parents=True)
        sibling = workspace / "some-other-project"
        sibling.mkdir()
        (sibling / ".orphaned_at").write_text("1789054301708")
        called = []
        monkeypatch.setattr(
            restart_service, "_has_open_file_handles",
            lambda path: called.append(path) or False,
        )

        result = restart_service.prune_orphaned_plugin_versions(current)

        assert result == {"removed": [], "skipped": []}
        assert sibling.exists()
        assert called == []  # 走査自体が起きていないこと(lsofすら呼ばれない)

    def test_treats_git_worktree_dot_git_file_as_git_checkout(self, monkeypatch, tmp_path):
        """worktreeの`.git`はファイル(gitdirへのポインタ)であり、ディレクトリとは限らない。
        `.exists()`で判定し`.is_dir()`を使わないことで、この形も検出できる。"""
        workspace = tmp_path / "workspace"
        current = workspace / "calm-worktree"
        current.mkdir(parents=True)
        (current / ".git").write_text("gitdir: /somewhere/.git/worktrees/calm-worktree\n")
        sibling = workspace / "some-other-project"
        sibling.mkdir()
        (sibling / ".orphaned_at").write_text("1789054301708")
        _no_open_handles(monkeypatch)

        result = restart_service.prune_orphaned_plugin_versions(current)

        assert result == {"removed": [], "skipped": []}
        assert sibling.exists()

    def test_handles_missing_versions_root_gracefully(self, tmp_path):
        # 親ディレクトリ自体が存在しないケースを模すため、存在しないパスの子を渡す
        current = tmp_path / "nonexistent_parent" / "calm-version"

        result = restart_service.prune_orphaned_plugin_versions(current)

        assert result == {"removed": [], "skipped": []}


def test_sync_dependencies_success(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="Resolved 42 packages\n", stderr="")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    result = restart_service.sync_dependencies(tmp_path)

    assert result.ok is True
    assert captured["cmd"] == ["uv", "sync", "--directory", str(tmp_path)]
    assert captured["kwargs"]["timeout"] == restart_service.DEFAULT_SYNC_TIMEOUT_SEC


def test_sync_dependencies_failure_returncode(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error: lock file mismatch")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    result = restart_service.sync_dependencies(tmp_path)

    assert result.ok is False
    assert "lock file mismatch" in result.detail


def test_sync_dependencies_timeout(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    result = restart_service.sync_dependencies(tmp_path, timeout_sec=1.0)

    assert result.ok is False
    assert "timed out" in result.detail


def test_restart_all_calls_in_expected_order_without_stopping_embedding(monkeypatch, tmp_path):
    """uv sync → キャッシュ掃除 → MCP再起動、の順で呼ばれ、既定ではembeddingサーバーを
    停止しないことを検証する。

    uv syncとキャッシュ掃除を旧サーバー稼働中に済ませ、
    kill〜起動〜監視のダウンタイムを最小化する狙いのため、この順序が重要。
    embeddingサーバーはコード変更頻度が低いため、MCP再起動のたびに巻き添えで
    停止させない(次に必要になったときlazy spawnされるだけで都度停止するメリットが薄い)。
    """
    call_order = []

    def fake_sync_dependencies(project_root):
        call_order.append("sync")
        return restart_service.SyncResult(True, 1.5, "synced")

    def fake_clean_caches(project_root):
        call_order.append("clean_caches")
        return {"removed_pycache_dirs": []}

    def fake_restart_mcp_server(project_root):
        call_order.append("restart_mcp_server")
        return restart_service.RestartResult(True, [1111], [2222], "restarted")

    def fake_stop_embedding_server():
        call_order.append("stop_embedding_server")
        return [9999]

    def fake_prune(project_root):
        call_order.append("prune_orphaned_plugin_versions")
        return {"removed": [], "skipped": []}

    monkeypatch.setattr(restart_service, "sync_dependencies", fake_sync_dependencies)
    monkeypatch.setattr(restart_service, "clean_caches", fake_clean_caches)
    monkeypatch.setattr(restart_service, "restart_mcp_server", fake_restart_mcp_server)
    monkeypatch.setattr(restart_service, "stop_embedding_server", fake_stop_embedding_server)
    monkeypatch.setattr(restart_service, "prune_orphaned_plugin_versions", fake_prune)
    monkeypatch.setattr(
        restart_service, "find_orphaned_plugin_cache_processes",
        lambda: {"stopped": [], "kept": []},
    )

    result = restart_service.restart_all(tmp_path)

    assert call_order == ["sync", "clean_caches", "restart_mcp_server", "prune_orphaned_plugin_versions"]
    assert result["uv_sync"] == {"ok": True, "duration_sec": 1.5, "detail": "synced"}
    assert result["mcp_server"]["ok"] is True
    assert result["embedding_server"] == {"stopped_pids": []}
    assert result["caches"] == {"removed_pycache_dirs": []}
    assert result["plugin_cache_prune"] == {"removed": [], "skipped": []}
    assert result["orphaned_processes"] == {"stopped": [], "kept": []}


def test_restart_all_stops_embedding_when_requested(monkeypatch, tmp_path):
    """restart_embedding=True を指定したときだけ stop_embedding_server が呼ばれる"""
    call_order = []

    monkeypatch.setattr(
        restart_service, "sync_dependencies",
        lambda project_root: restart_service.SyncResult(True, 0.1, "synced"),
    )
    monkeypatch.setattr(
        restart_service, "clean_caches",
        lambda project_root: {"removed_pycache_dirs": []},
    )

    def fake_restart_mcp_server(project_root):
        call_order.append("restart_mcp_server")
        return restart_service.RestartResult(True, [1111], [2222], "restarted")

    def fake_stop_embedding_server():
        call_order.append("stop_embedding_server")
        return [9999]

    monkeypatch.setattr(restart_service, "restart_mcp_server", fake_restart_mcp_server)
    monkeypatch.setattr(restart_service, "stop_embedding_server", fake_stop_embedding_server)
    monkeypatch.setattr(
        restart_service, "prune_orphaned_plugin_versions",
        lambda project_root: {"removed": [], "skipped": []},
    )
    monkeypatch.setattr(
        restart_service, "find_orphaned_plugin_cache_processes",
        lambda: {"stopped": [], "kept": []},
    )

    result = restart_service.restart_all(tmp_path, restart_embedding=True)

    assert call_order == ["restart_mcp_server", "stop_embedding_server"]
    assert result["embedding_server"] == {"stopped_pids": [9999]}


def test_restart_all_continues_to_mcp_restart_when_uv_sync_fails(monkeypatch, tmp_path):
    """uv syncが失敗しても後続のMCP再起動は試行し、成否は結果に含めて返す"""
    def fake_sync_dependencies(project_root):
        return restart_service.SyncResult(False, 0.1, "uv sync failed")

    mcp_restart_called = []

    def fake_restart_mcp_server(project_root):
        mcp_restart_called.append(project_root)
        return restart_service.RestartResult(True, [], [2222], "restarted")

    monkeypatch.setattr(restart_service, "sync_dependencies", fake_sync_dependencies)
    monkeypatch.setattr(restart_service, "clean_caches", lambda project_root: {"removed_pycache_dirs": []})
    monkeypatch.setattr(restart_service, "restart_mcp_server", fake_restart_mcp_server)
    monkeypatch.setattr(restart_service, "stop_embedding_server", lambda: [])
    monkeypatch.setattr(
        restart_service, "prune_orphaned_plugin_versions",
        lambda project_root: {"removed": [], "skipped": []},
    )
    monkeypatch.setattr(
        restart_service, "find_orphaned_plugin_cache_processes",
        lambda: {"stopped": [], "kept": []},
    )

    result = restart_service.restart_all(tmp_path)

    assert mcp_restart_called == [tmp_path]
    assert result["uv_sync"]["ok"] is False
    assert result["mcp_server"]["ok"] is True


def test_restart_all_skips_plugin_cache_prune_when_mcp_restart_fails(monkeypatch, tmp_path):
    """MCP再起動が失敗した場合、プラグインキャッシュの掃除は行わない
    (原因調査中にキャッシュディレクトリの状態まで変化させないため)"""
    prune_called = []

    monkeypatch.setattr(
        restart_service, "sync_dependencies",
        lambda project_root: restart_service.SyncResult(True, 0.1, "synced"),
    )
    monkeypatch.setattr(restart_service, "clean_caches", lambda project_root: {"removed_pycache_dirs": []})
    monkeypatch.setattr(
        restart_service, "restart_mcp_server",
        lambda project_root: restart_service.RestartResult(False, [1111], [], "did not come up"),
    )
    monkeypatch.setattr(restart_service, "stop_embedding_server", lambda: [])
    monkeypatch.setattr(
        restart_service, "prune_orphaned_plugin_versions",
        lambda project_root: prune_called.append(project_root) or {"removed": [], "skipped": []},
    )
    monkeypatch.setattr(
        restart_service, "find_orphaned_plugin_cache_processes",
        lambda: {"stopped": [], "kept": []},
    )

    result = restart_service.restart_all(tmp_path)

    assert prune_called == []
    assert result["mcp_server"]["ok"] is False
    assert result["plugin_cache_prune"] == {"removed": [], "skipped": []}


def test_restart_all_detects_orphaned_processes_even_when_mcp_restart_fails(monkeypatch, tmp_path):
    """孤児プロセスの検出・停止は、MCP再起動が失敗した場合でも行う
    (削除済みディレクトリから動き続けているプロセスは、再起動が失敗していても
    掃除する価値があるため、プラグインキャッシュ掃除とは異なり成否で出し分けない)"""
    monkeypatch.setattr(
        restart_service, "sync_dependencies",
        lambda project_root: restart_service.SyncResult(True, 0.1, "synced"),
    )
    monkeypatch.setattr(restart_service, "clean_caches", lambda project_root: {"removed_pycache_dirs": []})
    monkeypatch.setattr(
        restart_service, "restart_mcp_server",
        lambda project_root: restart_service.RestartResult(False, [1111], [], "did not come up"),
    )
    monkeypatch.setattr(restart_service, "stop_embedding_server", lambda: [])
    monkeypatch.setattr(
        restart_service, "prune_orphaned_plugin_versions",
        lambda project_root: {"removed": [], "skipped": []},
    )
    monkeypatch.setattr(
        restart_service, "find_orphaned_plugin_cache_processes",
        lambda: {"stopped": [{"pid": 71102, "command": "embedding_server", "cwd": "/deleted"}], "kept": []},
    )

    result = restart_service.restart_all(tmp_path)

    assert result["mcp_server"]["ok"] is False
    assert result["orphaned_processes"] == {
        "stopped": [{"pid": 71102, "command": "embedding_server", "cwd": "/deleted"}],
        "kept": [],
    }


class TestMainCli:
    """main(): --restart-embedding フラグのargparse配線を検証する"""

    def _run_main(self, monkeypatch, argv):
        captured = {}

        def fake_restart_all(project_root, *, restart_embedding=False):
            captured["restart_embedding"] = restart_embedding
            return {
                "uv_sync": {"ok": True, "duration_sec": 0.0, "detail": "synced"},
                "mcp_server": {"ok": True, "old_pids": [], "new_pids": [1], "detail": "restarted"},
                "embedding_server": {"stopped_pids": []},
                "caches": {"removed_pycache_dirs": []},
            }

        monkeypatch.setattr(restart_service, "restart_all", fake_restart_all)
        monkeypatch.setattr(restart_service.sys, "argv", ["restart_service.py"] + argv)
        restart_service.main()
        return captured

    def test_flag_absent_defaults_to_false(self, monkeypatch):
        """--restart-embedding を付けない場合、restart_all は restart_embedding=False で呼ばれる"""
        captured = self._run_main(monkeypatch, [])
        assert captured["restart_embedding"] is False

    def test_flag_present_passes_true(self, monkeypatch):
        """--restart-embedding を付けると restart_all は restart_embedding=True で呼ばれる"""
        captured = self._run_main(monkeypatch, ["--restart-embedding"])
        assert captured["restart_embedding"] is True

    def test_exits_nonzero_when_mcp_restart_fails(self, monkeypatch, capsys):
        """mcp_server.ok が False のとき sys.exit(1) する"""
        def fake_restart_all(project_root, *, restart_embedding=False):
            return {
                "uv_sync": {"ok": True, "duration_sec": 0.0, "detail": "synced"},
                "mcp_server": {"ok": False, "old_pids": [], "new_pids": [], "detail": "timed out"},
                "embedding_server": {"stopped_pids": []},
                "caches": {"removed_pycache_dirs": []},
            }

        monkeypatch.setattr(restart_service, "restart_all", fake_restart_all)
        monkeypatch.setattr(restart_service.sys, "argv", ["restart_service.py"])

        try:
            restart_service.main()
            raise AssertionError("SystemExitが発生しなかった")
        except SystemExit as e:
            assert e.code == 1

    def test_reconfigures_stdout_to_utf8(self, monkeypatch):
        """Windows既定のANSIコードページ下でもjson.dumps(ensure_ascii=False)の
        日本語出力がUnicodeEncodeErrorで落ちないよう、stdoutをUTF-8へ揃えること
        """
        calls = []

        class FakeStdout:
            def reconfigure(self, **kwargs):
                calls.append(kwargs)

            def write(self, *a, **kw):
                pass

            def flush(self):
                pass

        monkeypatch.setattr(restart_service.sys, "stdout", FakeStdout())
        self._run_main(monkeypatch, [])

        assert calls == [{"encoding": "utf-8"}]

    def test_status_flag_prints_status_without_restarting(self, monkeypatch, capsys):
        """--status は状態を表示するだけで、restart_all(実際の再起動)は呼ばない"""
        restart_called = []
        monkeypatch.setattr(restart_service, "restart_all", lambda *a, **kw: restart_called.append(1))
        monkeypatch.setattr(
            restart_service, "get_status",
            lambda: {"mcp_server": {"running": True}, "embedding_server": {"running": False}},
        )
        monkeypatch.setattr(restart_service.sys, "argv", ["restart_service.py", "--status"])

        restart_service.main()

        assert restart_called == []
        assert '"running": true' in capsys.readouterr().out

    def test_stop_flag_stops_without_restarting(self, monkeypatch):
        """--stop は停止するだけで、restart_all(新規プロセスの起動)は呼ばない"""
        restart_called = []
        monkeypatch.setattr(restart_service, "restart_all", lambda *a, **kw: restart_called.append(1))
        stop_calls = []

        def fake_stop_all(*, stop_embedding=False):
            stop_calls.append(stop_embedding)
            return {"mcp_server": {"stopped_pids": [1]}, "embedding_server": {"stopped_pids": []}}

        monkeypatch.setattr(restart_service, "stop_all", fake_stop_all)
        monkeypatch.setattr(restart_service.sys, "argv", ["restart_service.py", "--stop"])

        restart_service.main()

        assert restart_called == []
        assert stop_calls == [False]

    def test_stop_and_restart_embedding_flags_combine(self, monkeypatch):
        """--stop --restart-embedding は両方のサーバーを停止するだけで終了する"""
        stop_calls = []

        def fake_stop_all(*, stop_embedding=False):
            stop_calls.append(stop_embedding)
            return {"mcp_server": {"stopped_pids": []}, "embedding_server": {"stopped_pids": []}}

        monkeypatch.setattr(restart_service, "stop_all", fake_stop_all)
        monkeypatch.setattr(
            restart_service.sys, "argv", ["restart_service.py", "--stop", "--restart-embedding"],
        )

        restart_service.main()

        assert stop_calls == [True]

    def test_status_and_stop_are_mutually_exclusive(self, monkeypatch):
        monkeypatch.setattr(restart_service.sys, "argv", ["restart_service.py", "--status", "--stop"])

        with pytest.raises(SystemExit):
            restart_service.main()


def test_list_server_family_processes_filters_to_known_modules_and_excludes_launcher(monkeypatch):
    ps_output = "\n".join([
        "100 /opt/python -m src.main --transport http",
        "200 /opt/python -m src.infra.embedding_server",
        "300 uv run --directory /x/calm/1.0.0 python -m src.launcher",
        "400 /opt/python -m src.launcher",
        "500 /opt/python -m some.other.module",
    ])

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=ps_output, stderr="")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    found = restart_service._list_server_family_processes()

    assert [pid for pid, _ in found] == [100, 200]


def test_list_server_family_processes_requires_exact_module_argument(monkeypatch):
    ps_output = "\n".join([
        "600 /opt/python /x/src.main/tool.py",
        "700 /opt/python -m src.main_helper",
        "800 /opt/python -m src.main --transport http",
    ])

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=ps_output, stderr="")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert [pid for pid, _ in restart_service._list_server_family_processes()] == [800]


def test_list_server_family_processes_empty_on_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service._list_server_family_processes() == []


def test_process_cwd_parses_lsof_n_line(monkeypatch):
    captured_cmd = []

    def fake_run(cmd, **kwargs):
        captured_cmd.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout="p71102\nfcwd\nn/path/to/deleted/version\n", stderr="",
        )

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service._process_cwd(71102) == "/path/to/deleted/version"
    assert captured_cmd == [["lsof", "-a", "-p", "71102", "-d", "cwd", "-Fn"]]


def test_process_cwd_none_on_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service._process_cwd(71102) is None


def test_process_cwd_none_when_lsof_reports_nothing(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service._process_cwd(71102) is None


class TestFindOrphanedPluginCacheProcesses:
    """削除済みプラグインディレクトリから動いているサーバー系プロセスの検出・停止を検証する。"""

    def test_stops_process_whose_cwd_directory_no_longer_exists(self, monkeypatch, tmp_path):
        deleted_dir = tmp_path / "deleted_version"  # mkdirしない(=存在しない)

        monkeypatch.setattr(
            restart_service, "_list_server_family_processes",
            lambda: [(71102, "/opt/python -m src.infra.embedding_server")],
        )
        monkeypatch.setattr(restart_service, "_process_cwd", lambda pid: str(deleted_dir))
        killed = []
        monkeypatch.setattr(restart_service, "kill_pids", lambda pids: killed.extend(pids))

        result = restart_service.find_orphaned_plugin_cache_processes()

        assert result == {
            "stopped": [{
                "pid": 71102,
                "command": "/opt/python -m src.infra.embedding_server",
                "cwd": str(deleted_dir),
            }],
            "kept": [],
        }
        assert killed == [71102]

    def test_keeps_process_whose_cwd_directory_still_exists(self, monkeypatch, tmp_path):
        live_dir = tmp_path / "current_version"
        live_dir.mkdir()

        monkeypatch.setattr(
            restart_service, "_list_server_family_processes",
            lambda: [(66799, "/opt/python -m src.main --transport http")],
        )
        monkeypatch.setattr(restart_service, "_process_cwd", lambda pid: str(live_dir))
        killed = []
        monkeypatch.setattr(restart_service, "kill_pids", lambda pids: killed.extend(pids))

        result = restart_service.find_orphaned_plugin_cache_processes()

        assert result == {
            "stopped": [],
            "kept": [{
                "pid": 66799,
                "command": "/opt/python -m src.main --transport http",
                "cwd": str(live_dir),
                "reason": "directory still exists",
            }],
        }
        assert killed == []

    def test_keeps_process_when_cwd_unknown(self, monkeypatch):
        """cwdが判定できない(lsofタイムアウト等)場合は安全側に倒して停止しない"""
        monkeypatch.setattr(
            restart_service, "_list_server_family_processes",
            lambda: [(12345, "/opt/python -m src.main --transport http")],
        )
        monkeypatch.setattr(restart_service, "_process_cwd", lambda pid: None)
        killed = []
        monkeypatch.setattr(restart_service, "kill_pids", lambda pids: killed.extend(pids))

        result = restart_service.find_orphaned_plugin_cache_processes()

        assert result == {
            "stopped": [],
            "kept": [{
                "pid": 12345,
                "command": "/opt/python -m src.main --transport http",
                "cwd": None,
                "reason": "cwd unknown",
            }],
        }
        assert killed == []

    def test_noop_when_no_candidates(self, monkeypatch):
        monkeypatch.setattr(restart_service, "_list_server_family_processes", lambda: [])
        killed = []
        monkeypatch.setattr(restart_service, "kill_pids", lambda pids: killed.extend(pids))

        assert restart_service.find_orphaned_plugin_cache_processes() == {"stopped": [], "kept": []}
        assert killed == []
