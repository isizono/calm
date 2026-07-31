"""src/services/restart_service.py のユニットテスト

subprocess呼び出し(lsof/ps/kill/Popen)を外部境界としてmonkeypatchし、
プロセス入れ替え判定ロジック・キャッシュ削除の契約を検証する。
"""
import subprocess

from src.services import restart_service


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


def test_process_start_signature_returns_stripped_lstart_output(monkeypatch):
    captured_cmd = []

    def fake_run(cmd, **kwargs):
        captured_cmd.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="  Thu Jul 24 09:32:04 2026  \n", stderr="")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    signature = restart_service.process_start_signature(1234)

    assert signature == "Thu Jul 24 09:32:04 2026"
    assert captured_cmd == [["ps", "-o", "lstart=", "-p", "1234"]]


def test_process_start_signature_none_for_dead_process(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ps: 1234: No such process")

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service.process_start_signature(1234) is None


def test_process_start_signature_none_on_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

    assert restart_service.process_start_signature(1234) is None


def test_process_alive_false_when_process_lookup_error(monkeypatch):
    def fake_kill(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(restart_service.os, "kill", fake_kill)

    assert restart_service._process_alive(1234) is False


def test_process_alive_true_when_permission_denied(monkeypatch):
    """権限エラーは「シグナルは送れないが存在はする」ことを意味するため生存扱いにする"""
    def fake_kill(pid, sig):
        raise PermissionError

    monkeypatch.setattr(restart_service.os, "kill", fake_kill)

    assert restart_service._process_alive(1234) is True


def test_kill_pids_sends_sigterm_only_when_process_dies_promptly(monkeypatch):
    """SIGTERMだけで終了する場合はSIGKILLへエスカレーションしない"""
    signals_sent = []

    def fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError  # 生存確認: SIGTERM後すぐ死んだ想定
        signals_sent.append((pid, sig))

    monkeypatch.setattr(restart_service.os, "kill", fake_kill)

    restart_service.kill_pids([1234])

    assert signals_sent == [(1234, restart_service.signal.SIGTERM)]


def test_kill_pids_escalates_to_sigkill_when_process_survives_sigterm(monkeypatch):
    """SIGTERMを送っても生存し続けるプロセスにはSIGKILLを送る"""
    signals_sent = []

    def fake_kill(pid, sig):
        signals_sent.append((pid, sig))  # sig=0(生存確認)も例外を投げず「生存」を返す

    monkeypatch.setattr(restart_service.os, "kill", fake_kill)
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)

    restart_service.kill_pids([1234], escalate_after_sec=0, poll_interval_sec=0)

    assert (1234, restart_service.signal.SIGTERM) in signals_sent
    assert (1234, restart_service.signal.SIGKILL) in signals_sent


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

    monkeypatch.setattr(restart_service, "find_listen_pids", fake_find_listen_pids)
    monkeypatch.setattr(
        restart_service, "process_start_signature",
        lambda pid: {1111: "old-sig", 2222: "new-sig"}.get(pid),
    )
    monkeypatch.setattr(restart_service, "kill_pids", fake_kill_pids)
    monkeypatch.setattr(restart_service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)

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

    monkeypatch.setattr(restart_service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)

    result = restart_service.restart_mcp_server(tmp_path, poll_interval_sec=0)

    assert result.ok is True
    assert result.old_pids == []
    assert killed == []


def test_restart_mcp_server_times_out_when_server_never_comes_up(monkeypatch, tmp_path):
    monkeypatch.setattr(restart_service, "find_listen_pids", lambda port: [])
    monkeypatch.setattr(restart_service, "kill_pids", lambda pids: None)
    monkeypatch.setattr(restart_service.subprocess, "Popen", lambda cmd, **kwargs: None)
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)

    result = restart_service.restart_mcp_server(
        tmp_path, start_timeout_sec=0, poll_interval_sec=0, kill_wait_sec=0,
    )

    assert result.ok is False
    assert result.new_pids == []
    assert "did not come up on port 52837" in result.detail


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


def test_clean_caches_removes_plugin_cache_and_pycache(monkeypatch, tmp_path):
    plugin_cache = tmp_path / "plugin_cache"
    plugin_cache.mkdir()
    (plugin_cache / "dummy.txt").write_text("x")
    monkeypatch.setattr(restart_service, "PLUGIN_CACHE_DIR", plugin_cache)

    project_root = tmp_path / "project"
    pycache = project_root / "src" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "foo.pyc").write_text("x")

    result = restart_service.clean_caches(project_root)

    assert result["removed_plugin_cache"] is True
    assert not plugin_cache.exists()
    assert result["removed_pycache_dirs"] == [str(pycache)]
    assert not pycache.exists()


def test_clean_caches_handles_missing_plugin_cache_dir(monkeypatch, tmp_path):
    plugin_cache = tmp_path / "nonexistent"
    monkeypatch.setattr(restart_service, "PLUGIN_CACHE_DIR", plugin_cache)

    project_root = tmp_path / "project"
    project_root.mkdir()

    result = restart_service.clean_caches(project_root)

    assert result["removed_plugin_cache"] is False
    assert result["removed_pycache_dirs"] == []
