"""relay 接続設定（src/services/relay/config.py）の unit test。"""
import json

import pytest

from src.services.relay import config
from src.services.relay.config import RelayConfigError


@pytest.fixture(autouse=True)
def _clean_relay_env(monkeypatch):
    for key in ("RELAY_BASE_URL", "RELAY_BEARER_TOKEN", "RELAY_STATE_DIR", "RELAY_IDENTITY"):
        monkeypatch.delenv(key, raising=False)


class TestDefaults:
    def test_base_url_default(self):
        assert config.get_base_url() == "http://localhost:8770"

    def test_identity_default(self):
        assert config.get_identity() == "cc-memory"

    def test_state_dir_default_under_home(self):
        state_dir = config.get_state_dir()
        assert state_dir.name == "relay"
        assert state_dir.parent.name == ".cc-memory"

    def test_sub_dirs(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
        assert config.subscriptions_dir() == tmp_path / "subscriptions"
        assert config.inbox_dir() == tmp_path / "inbox"
        assert config.sessions_dir() == tmp_path / "sessions"


class TestEnvOverrides:
    def test_base_url_env(self, monkeypatch):
        monkeypatch.setenv("RELAY_BASE_URL", "http://example.test:9999")
        assert config.get_base_url() == "http://example.test:9999"

    def test_identity_env(self, monkeypatch):
        monkeypatch.setenv("RELAY_IDENTITY", "my-server")
        assert config.get_identity() == "my-server"

    def test_state_dir_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "custom"))
        assert config.get_state_dir() == tmp_path / "custom"


class TestToken:
    def test_get_token_none_when_unset(self):
        assert config.get_token() is None

    def test_require_token_raises_with_setup_instructions(self):
        with pytest.raises(RelayConfigError) as excinfo:
            config.require_token()
        assert "redeem" in str(excinfo.value)

    def test_require_token_returns_value(self, monkeypatch):
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "secret")
        assert config.require_token() == "secret"


class TestCredentialFileFallback:
    """env → credential.json → 既定 のフォールバック順を検証する。"""

    def _write_credential(self, tmp_path, **overrides):
        data = {
            "base_url": "http://127.0.0.1:8770",
            "identity": "cc-memory",
            "bearer_token": "bt_from_file",
            "issued_at": "2026-07-08T00:00:00Z",
            "expires_at": None,
        }
        data.update(overrides)
        (tmp_path / "credential.json").write_text(json.dumps(data))
        return data

    def test_get_token_reads_credential_file_when_env_unset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
        self._write_credential(tmp_path)
        assert config.get_token() == "bt_from_file"

    def test_get_token_env_overrides_credential_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
        self._write_credential(tmp_path)
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "bt_from_env")
        assert config.get_token() == "bt_from_env"

    def test_get_token_none_when_no_env_and_no_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
        assert config.get_token() is None

    def test_get_token_none_when_credential_file_missing_bearer_token(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
        self._write_credential(tmp_path, bearer_token=None)
        assert config.get_token() is None

    def test_get_token_none_when_credential_file_malformed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
        (tmp_path / "credential.json").write_text("not valid json")
        assert config.get_token() is None

    def test_get_base_url_reads_credential_file_when_env_unset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
        self._write_credential(tmp_path, base_url="http://127.0.0.1:8770")
        assert config.get_base_url() == "http://127.0.0.1:8770"

    def test_get_base_url_env_overrides_credential_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
        self._write_credential(tmp_path, base_url="http://127.0.0.1:8770")
        monkeypatch.setenv("RELAY_BASE_URL", "http://example.test:9999")
        assert config.get_base_url() == "http://example.test:9999"

    def test_get_base_url_default_when_no_env_and_no_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
        assert config.get_base_url() == "http://localhost:8770"

    def test_get_identity_reads_credential_file_when_env_unset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
        self._write_credential(tmp_path, identity="my-agent")
        assert config.get_identity() == "my-agent"

    def test_get_identity_env_overrides_credential_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
        self._write_credential(tmp_path, identity="my-agent")
        monkeypatch.setenv("RELAY_IDENTITY", "env-agent")
        assert config.get_identity() == "env-agent"

    def test_get_identity_default_when_no_env_and_no_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
        assert config.get_identity() == "cc-memory"
