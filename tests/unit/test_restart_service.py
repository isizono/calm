"""src/services/restart_service.py のユニットテスト

subprocess呼び出し(lsof/ps/kill/Popen)を外部境界としてmonkeypatchし、
プロセス入れ替え判定ロジック・キャッシュ削除の契約を検証する。
"""
import subprocess
from types import SimpleNamespace

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
        return SimpleNamespace(pid=2222)

    monkeypatch.setattr(restart_service, "find_listen_pids", fake_find_listen_pids)
    monkeypatch.setattr(
        restart_service, "process_start_signature",
        lambda pid: {1111: "old-sig", 2222: "new-sig"}.get(pid),
    )
    monkeypatch.setattr(restart_service, "kill_pids", fake_kill_pids)
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

    monkeypatch.setattr(restart_service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(restart_service.time, "sleep", lambda _: None)
    monkeypatch.setattr(restart_service, "LAUNCHER_LOG_PATH", tmp_path / "logs" / "restart_launcher.log")

    result = restart_service.restart_mcp_server(tmp_path, poll_interval_sec=0)

    assert result.ok is True
    assert result.old_pids == []
    assert killed == []


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

    monkeypatch.setattr(restart_service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(restart_service, "LAUNCHER_LOG_PATH", tmp_path / "logs" / "restart_launcher.log")

    result = restart_service.restart_mcp_server(tmp_path)

    assert result.ok is True
    assert result.old_pids == [1111]
    assert result.new_pids == [2222]
    assert process_alive[1111] is False


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


def test_restart_mcp_server_times_out_when_server_never_comes_up(monkeypatch, tmp_path):
    monkeypatch.setattr(restart_service, "find_listen_pids", lambda port: [])
    monkeypatch.setattr(restart_service, "kill_pids", lambda pids: None)
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


def test_restart_mcp_server_ignores_process_lookup_error_when_killing_process_group(monkeypatch, tmp_path):
    """killpgが対象プロセスの消滅を示すProcessLookupErrorを送出しても後始末全体は失敗にしない"""
    monkeypatch.setattr(restart_service, "find_listen_pids", lambda port: [])
    monkeypatch.setattr(restart_service, "kill_pids", lambda pids: None)
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


def test_restart_mcp_server_writes_launcher_output_to_log_file(monkeypatch, tmp_path):
    """launcherのstdout/stderrをDEVNULLではなくログファイルへまとめる"""
    log_path = tmp_path / "logs" / "restart_launcher.log"
    monkeypatch.setattr(restart_service, "LAUNCHER_LOG_PATH", log_path)

    state = {"new_server_started": False}

    def fake_find_listen_pids(port):
        return [2222] if state["new_server_started"] else []

    monkeypatch.setattr(restart_service, "find_listen_pids", fake_find_listen_pids)
    monkeypatch.setattr(restart_service, "process_start_signature", lambda pid: "sig")

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


def test_restart_all_calls_in_expected_order(monkeypatch, tmp_path):
    """uv sync → キャッシュ掃除 → MCP再起動 → embedding停止、の順で呼ばれることを検証する。

    uv syncとキャッシュ掃除を旧サーバー稼働中に済ませ、
    kill〜起動〜監視のダウンタイムを最小化する狙いのため、この順序が重要。
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
        return []

    monkeypatch.setattr(restart_service, "sync_dependencies", fake_sync_dependencies)
    monkeypatch.setattr(restart_service, "clean_caches", fake_clean_caches)
    monkeypatch.setattr(restart_service, "restart_mcp_server", fake_restart_mcp_server)
    monkeypatch.setattr(restart_service, "stop_embedding_server", fake_stop_embedding_server)

    result = restart_service.restart_all(tmp_path)

    assert call_order == ["sync", "clean_caches", "restart_mcp_server", "stop_embedding_server"]
    assert result["uv_sync"] == {"ok": True, "duration_sec": 1.5, "detail": "synced"}
    assert result["mcp_server"]["ok"] is True
    assert result["embedding_server"] == {"stopped_pids": []}
    assert result["caches"] == {"removed_pycache_dirs": []}


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

    result = restart_service.restart_all(tmp_path)

    assert mcp_restart_called == [tmp_path]
    assert result["uv_sync"]["ok"] is False
    assert result["mcp_server"]["ok"] is True
