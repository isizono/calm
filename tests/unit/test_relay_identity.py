"""relay 呼び出し元の安定 identity 解決（src.services.relay.identity）のユニットテスト。

bridge identity ヘッダ優先 + ctx.session_id フォールバックの契約に加え、
SessionStart hook 用の祖先 pid チェーンによる identity 解決
（ancestor_pids / register_launcher_session / resolve_identity_by_ancestry）を検証する。
"""
import json
import os

import pytest

from src.services.relay import config as relay_config
from src.services.relay import identity as relay_identity


class TestGetRelayIdentity:
    def test_returns_header_value_when_present(self, monkeypatch):
        """ヘッダが存在する場合はその値を返し、ctx.session_idにはフォールバックしない"""
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers",
            lambda: {relay_identity.BRIDGE_SESSION_HEADER: "bridge-uuid-1"},
        )
        called = {"fallback": False}

        def fake_fallback():
            called["fallback"] = True
            return "ephemeral-session-id"

        monkeypatch.setattr(relay_identity, "_ephemeral_session_id", fake_fallback)
        assert relay_identity.get_relay_identity() == "bridge-uuid-1"
        assert called["fallback"] is False

    def test_falls_back_when_header_absent(self, monkeypatch):
        """ヘッダが無い場合は ctx.session_id（_ephemeral_session_id）にフォールバックする"""
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers", lambda: {}
        )
        monkeypatch.setattr(
            relay_identity, "_ephemeral_session_id", lambda: "ephemeral-session-id"
        )
        assert relay_identity.get_relay_identity() == "ephemeral-session-id"

    def test_falls_back_when_header_is_blank(self, monkeypatch):
        """ヘッダが空文字列・空白のみの場合もフォールバックする（安全側）"""
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers",
            lambda: {relay_identity.BRIDGE_SESSION_HEADER: "   "},
        )
        monkeypatch.setattr(
            relay_identity, "_ephemeral_session_id", lambda: "ephemeral-session-id"
        )
        assert relay_identity.get_relay_identity() == "ephemeral-session-id"

    def test_falls_back_when_get_http_headers_import_fails(self, monkeypatch):
        """get_http_headers自体のimportが失敗しても例外を投げずフォールバックする"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "fastmcp.server.dependencies":
                raise ImportError("simulated import failure")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.setattr(
            relay_identity, "_ephemeral_session_id", lambda: "ephemeral-session-id"
        )
        assert relay_identity.get_relay_identity() == "ephemeral-session-id"

    def test_falls_back_when_get_http_headers_call_raises(self, monkeypatch):
        """import自体は成功しても、HTTPリクエストコンテキスト外呼び出し等で
        get_http_headers()の呼び出しが例外を投げた場合もフォールバックする"""

        def raising_get_http_headers():
            raise RuntimeError("no active HTTP request context")

        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers",
            raising_get_http_headers,
        )
        monkeypatch.setattr(
            relay_identity, "_ephemeral_session_id", lambda: "ephemeral-session-id"
        )
        assert relay_identity.get_relay_identity() == "ephemeral-session-id"

    def test_strips_whitespace_from_header_value(self, monkeypatch):
        """ヘッダ値の前後空白は取り除いて返す"""
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers",
            lambda: {relay_identity.BRIDGE_SESSION_HEADER: "  bridge-uuid-2  "},
        )
        assert relay_identity.get_relay_identity() == "bridge-uuid-2"


def _fake_ppid_chain(monkeypatch, graph: dict[int, int | None]):
    """{pid: ppid} の固定マップで _get_ppid をモックする（実プロセスに依存しない）。"""
    monkeypatch.setattr(relay_identity, "_get_ppid", lambda pid: graph.get(pid))


@pytest.fixture
def sessions_state_dir(tmp_path, monkeypatch):
    """get_state_dir() をtmp_pathに差し替え、sessions_dir()を隔離する。"""
    monkeypatch.setattr(relay_config, "get_state_dir", lambda: tmp_path)
    return tmp_path


class TestAncestorPids:
    def test_walks_chain_until_pid_1(self, monkeypatch):
        # 100 -> 50 -> 10 -> 1（1到達で打ち切り、1自体はリストに含めない）
        _fake_ppid_chain(monkeypatch, {100: 50, 50: 10, 10: 1})
        assert relay_identity.ancestor_pids(100) == [50, 10]

    def test_respects_max_depth(self, monkeypatch):
        # 5段より深い連鎖でも max_depth=2 なら2段で打ち切る
        _fake_ppid_chain(monkeypatch, {100: 50, 50: 10, 10: 5, 5: 2, 2: 1})
        assert relay_identity.ancestor_pids(100, max_depth=2) == [50, 10]

    def test_stops_when_ppid_lookup_fails(self, monkeypatch):
        # 50のppidが取得不能（プロセス消滅等）ならそこで打ち切る
        _fake_ppid_chain(monkeypatch, {100: 50})
        assert relay_identity.ancestor_pids(100) == [50]

    def test_empty_when_immediate_lookup_fails(self, monkeypatch):
        _fake_ppid_chain(monkeypatch, {})
        assert relay_identity.ancestor_pids(999) == []


class TestRegisterLauncherSession:
    def test_writes_registration_file_with_expected_fields(
        self, sessions_state_dir, monkeypatch
    ):
        _fake_ppid_chain(monkeypatch, {4321: 999, 999: 1})
        path = relay_identity.register_launcher_session("launcher-uuid-1", pid=4321)
        assert path == sessions_state_dir / "sessions" / "launcher-4321.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["session_id"] == "launcher-uuid-1"
        assert data["pid"] == 4321
        assert data["ancestor_pids"] == [999]
        assert "created_at" in data

    def test_registration_file_is_owner_only_permission(
        self, sessions_state_dir, monkeypatch
    ):
        _fake_ppid_chain(monkeypatch, {})
        path = relay_identity.register_launcher_session("launcher-uuid-1", pid=4321)
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600

    def test_gcs_stale_registration_before_writing(self, sessions_state_dir, monkeypatch):
        """生存していないpidの登録ファイルは新規書込前にGCされる"""
        sessions_dir = sessions_state_dir / "sessions"
        sessions_dir.mkdir(parents=True)
        stale = sessions_dir / "launcher-1111.json"
        stale.write_text(
            json.dumps({"session_id": "dead", "pid": 1111, "ancestor_pids": []}),
            encoding="utf-8",
        )
        monkeypatch.setattr(relay_identity, "is_process_alive", lambda pid: pid != 1111)
        _fake_ppid_chain(monkeypatch, {})
        relay_identity.register_launcher_session("launcher-uuid-2", pid=2222)
        assert not stale.exists()
        assert (sessions_dir / "launcher-2222.json").exists()

    def test_gcs_corrupted_registration_file(self, sessions_state_dir, monkeypatch):
        sessions_dir = sessions_state_dir / "sessions"
        sessions_dir.mkdir(parents=True)
        broken = sessions_dir / "launcher-9999.json"
        broken.write_text("not json", encoding="utf-8")
        _fake_ppid_chain(monkeypatch, {})
        relay_identity.register_launcher_session("launcher-uuid-3", pid=3333)
        assert not broken.exists()

    def test_returns_none_on_write_failure(self, sessions_state_dir, monkeypatch):
        """書込失敗（OSError）は例外を投げずNoneを返す"""
        _fake_ppid_chain(monkeypatch, {})

        def raise_mkstemp(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(relay_identity.tempfile, "mkstemp", raise_mkstemp)
        assert relay_identity.register_launcher_session("launcher-uuid-4", pid=4444) is None


class TestUnregisterLauncherSession:
    def test_removes_registration_file(self, sessions_state_dir, monkeypatch):
        _fake_ppid_chain(monkeypatch, {})
        relay_identity.register_launcher_session("launcher-uuid-5", pid=5555)
        path = sessions_state_dir / "sessions" / "launcher-5555.json"
        assert path.exists()
        relay_identity.unregister_launcher_session(pid=5555)
        assert not path.exists()

    def test_no_error_when_file_absent(self, sessions_state_dir):
        # 存在しないpidを解除しても例外にならない
        relay_identity.unregister_launcher_session(pid=99999999)


class TestResolveIdentityByAncestry:
    def test_resolves_when_common_ancestor_found(self, sessions_state_dir, monkeypatch):
        # hook(自分)の祖先: 300 -> 200 -> 1。launcherの祖先: 400 -> 200 -> 1。
        # 共通祖先200を介して一致する。
        _fake_ppid_chain(monkeypatch, {300: 200, 200: 1})
        monkeypatch.setattr(relay_identity, "is_process_alive", lambda pid: True)
        sessions_dir = sessions_state_dir / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "launcher-400.json").write_text(
            json.dumps(
                {
                    "session_id": "launcher-uuid-match",
                    "pid": 400,
                    "ancestor_pids": [200, 1],
                    "created_at": "2026-07-08T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        assert relay_identity.resolve_identity_by_ancestry(pid=300) == "launcher-uuid-match"

    def test_returns_none_when_no_common_ancestor(self, sessions_state_dir, monkeypatch):
        _fake_ppid_chain(monkeypatch, {300: 999})
        monkeypatch.setattr(relay_identity, "is_process_alive", lambda pid: True)
        sessions_dir = sessions_state_dir / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "launcher-400.json").write_text(
            json.dumps(
                {"session_id": "unrelated", "pid": 400, "ancestor_pids": [200]}
            ),
            encoding="utf-8",
        )
        assert relay_identity.resolve_identity_by_ancestry(pid=300) is None

    def test_returns_none_when_sessions_dir_missing(self, sessions_state_dir, monkeypatch):
        _fake_ppid_chain(monkeypatch, {300: 200})
        assert relay_identity.resolve_identity_by_ancestry(pid=300) is None

    def test_returns_none_when_own_ancestors_empty(self, sessions_state_dir, monkeypatch):
        _fake_ppid_chain(monkeypatch, {})
        sessions_dir = sessions_state_dir / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "launcher-400.json").write_text(
            json.dumps({"session_id": "x", "pid": 400, "ancestor_pids": [1]}),
            encoding="utf-8",
        )
        assert relay_identity.resolve_identity_by_ancestry(pid=999999) is None

    def test_skips_registration_whose_pid_is_dead(self, sessions_state_dir, monkeypatch):
        """共通祖先があっても登録元launcherのpidが死んでいれば候補から除外する"""
        _fake_ppid_chain(monkeypatch, {300: 200})
        monkeypatch.setattr(relay_identity, "is_process_alive", lambda pid: False)
        sessions_dir = sessions_state_dir / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "launcher-400.json").write_text(
            json.dumps(
                {"session_id": "dead-launcher", "pid": 400, "ancestor_pids": [200]}
            ),
            encoding="utf-8",
        )
        assert relay_identity.resolve_identity_by_ancestry(pid=300) is None

    def test_picks_most_recent_when_multiple_candidates_match(
        self, sessions_state_dir, monkeypatch
    ):
        """複数の登録ファイルが共通祖先を持つ場合、created_atが新しい方を採用する"""
        _fake_ppid_chain(monkeypatch, {300: 200})
        monkeypatch.setattr(relay_identity, "is_process_alive", lambda pid: True)
        sessions_dir = sessions_state_dir / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "launcher-400.json").write_text(
            json.dumps(
                {
                    "session_id": "older",
                    "pid": 400,
                    "ancestor_pids": [200],
                    "created_at": "2026-07-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        (sessions_dir / "launcher-401.json").write_text(
            json.dumps(
                {
                    "session_id": "newer",
                    "pid": 401,
                    "ancestor_pids": [200],
                    "created_at": "2026-07-08T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        assert relay_identity.resolve_identity_by_ancestry(pid=300) == "newer"

    def test_ignores_malformed_registration_file(self, sessions_state_dir, monkeypatch):
        _fake_ppid_chain(monkeypatch, {300: 200})
        monkeypatch.setattr(relay_identity, "is_process_alive", lambda pid: True)
        sessions_dir = sessions_state_dir / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "launcher-400.json").write_text("not json", encoding="utf-8")
        assert relay_identity.resolve_identity_by_ancestry(pid=300) is None
