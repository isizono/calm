"""relay 接続設定（src/services/relay/config.py）の unit test。"""
import pytest

from src.services.relay import config
from src.services.relay.config import RelayConfigError


@pytest.fixture(autouse=True)
def _clean_relay_env(monkeypatch):
    for key in ("RELAY_BASE_URL", "RELAY_TOKEN", "RELAY_STATE_DIR", "RELAY_IDENTITY"):
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
        assert "RELAY_TOKEN" in str(excinfo.value)

    def test_require_token_returns_value(self, monkeypatch):
        monkeypatch.setenv("RELAY_TOKEN", "secret")
        assert config.require_token() == "secret"
