"""hooks/session_start_hook.py の版チェックセクション
（_build_version_check_section / _fetch_running_server_version）専用のユニットテスト。

インストール版の解決（resolve_installed_plugin_root）と稼働中サーバーの
`/health`応答（_fetch_running_server_version）の両方をmockし、一致・不一致・
判定不能（いずれかが解決不能）の分岐を検証する。
"""
from hooks.session_start_hook import (
    _build_version_check_section,
    _fetch_running_server_version,
)
from pathlib import Path

from hooks import session_start_hook
from src.infra import loopback_http


def test_returns_empty_when_versions_match(monkeypatch):
    monkeypatch.setattr(
        session_start_hook, "resolve_installed_plugin_root", lambda root: Path("/plugins/cache/calm/calm/abc123")
    )
    monkeypatch.setattr(session_start_hook, "_fetch_running_server_version", lambda: "abc123")

    assert _build_version_check_section(conn=None) == ""


def test_warns_when_versions_differ(monkeypatch):
    monkeypatch.setattr(
        session_start_hook, "resolve_installed_plugin_root", lambda root: Path("/plugins/cache/calm/calm/newver")
    )
    monkeypatch.setattr(session_start_hook, "_fetch_running_server_version", lambda: "oldver")

    result = _build_version_check_section(conn=None)

    assert "/restart" in result
    assert result.endswith("\n")


def test_silent_when_installed_version_unresolvable(monkeypatch):
    monkeypatch.setattr(session_start_hook, "resolve_installed_plugin_root", lambda root: None)
    monkeypatch.setattr(session_start_hook, "_fetch_running_server_version", lambda: "oldver")

    assert _build_version_check_section(conn=None) == ""


def test_silent_when_running_version_unresolvable(monkeypatch):
    """サーバーに繋がらない・/healthにversionキーが無い等は判定不能として何も出さない"""
    monkeypatch.setattr(
        session_start_hook, "resolve_installed_plugin_root", lambda root: Path("/plugins/cache/calm/calm/abc123")
    )
    monkeypatch.setattr(session_start_hook, "_fetch_running_server_version", lambda: None)

    assert _build_version_check_section(conn=None) == ""


def test_fetch_running_server_version_returns_none_on_connection_error(monkeypatch):
    def fake_urlopen(url, timeout=None):
        raise ConnectionRefusedError("no server")

    monkeypatch.setattr(loopback_http.NO_PROXY_OPENER, "open", fake_urlopen)

    assert _fetch_running_server_version() is None


def test_fetch_running_server_version_returns_none_when_version_key_missing(monkeypatch):
    """versionキーを返さない旧版サーバーへの接続は判定不能として扱う"""
    import json
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"status": "ok", "pid": 1}).encode("utf-8")

    monkeypatch.setattr(loopback_http.NO_PROXY_OPENER, "open", lambda url, timeout=None: FakeResponse())

    assert _fetch_running_server_version() is None


def test_fetch_running_server_version_returns_value_on_success(monkeypatch):
    import json
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"status": "ok", "version": "abc123"}).encode("utf-8")

    monkeypatch.setattr(loopback_http.NO_PROXY_OPENER, "open", lambda url, timeout=None: FakeResponse())

    assert _fetch_running_server_version() == "abc123"
