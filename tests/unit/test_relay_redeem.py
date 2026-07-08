"""招待URL redeem CLI（src/services/relay/redeem.py）の unit test。"""
import io
import json
import stat

import httpx
import pytest

from src.services.relay import redeem

VALID_URL = "http://127.0.0.1:8770/invitations/redeem#v=1&t=it_abc123"


@pytest.fixture(autouse=True)
def _clean_relay_env(monkeypatch):
    for key in ("RELAY_BASE_URL", "RELAY_BEARER_TOKEN", "RELAY_STATE_DIR", "RELAY_IDENTITY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "relay-state"
    monkeypatch.setenv("RELAY_STATE_DIR", str(d))
    return d


class TestParseInviteUrl:
    def test_valid_url(self):
        endpoint, base_url, token = redeem._parse_invite_url(VALID_URL)
        assert endpoint == "http://127.0.0.1:8770/invitations/redeem"
        assert base_url == "http://127.0.0.1:8770"
        assert token == "it_abc123"

    def test_strips_surrounding_whitespace(self):
        endpoint, base_url, token = redeem._parse_invite_url(f"  {VALID_URL}  \n")
        assert token == "it_abc123"

    def test_missing_fragment_rejected(self):
        with pytest.raises(redeem.RedeemError):
            redeem._parse_invite_url("http://127.0.0.1:8770/invitations/redeem")

    def test_missing_token_in_fragment_rejected(self):
        with pytest.raises(redeem.RedeemError):
            redeem._parse_invite_url("http://127.0.0.1:8770/invitations/redeem#v=1")

    def test_missing_scheme_rejected(self):
        with pytest.raises(redeem.RedeemError):
            redeem._parse_invite_url("127.0.0.1:8770/invitations/redeem#v=1&t=it_abc123")

    def test_missing_path_rejected(self):
        with pytest.raises(redeem.RedeemError):
            redeem._parse_invite_url("http://127.0.0.1:8770#v=1&t=it_abc123")

    def test_garbage_string_rejected(self):
        with pytest.raises(redeem.RedeemError):
            redeem._parse_invite_url("not a url at all")


class TestMainHappyPath:
    def test_reads_url_from_stdin_and_writes_credential_atomically(
        self, monkeypatch, capsys, state_dir
    ):
        monkeypatch.setattr("sys.stdin", io.StringIO(VALID_URL + "\n"))

        def fake_post(url, *, json, timeout):
            assert url == "http://127.0.0.1:8770/invitations/redeem"
            assert json == {"invite_token": "it_abc123"}
            return httpx.Response(
                200,
                json={
                    "bearer_token": "bt_secret",
                    "identity": "cc-memory",
                    "expires_at": None,
                },
            )

        monkeypatch.setattr(httpx, "post", fake_post)

        exit_code = redeem.main()

        assert exit_code == 0
        cred_path = state_dir / redeem.CREDENTIAL_FILENAME
        assert cred_path.exists()
        data = json.loads(cred_path.read_text())
        assert data["bearer_token"] == "bt_secret"
        assert data["identity"] == "cc-memory"
        assert data["base_url"] == "http://127.0.0.1:8770"
        assert data["expires_at"] is None
        assert "issued_at" in data

        # credential.json は 0600、親 dir は 0700。
        file_mode = stat.S_IMODE(cred_path.stat().st_mode)
        dir_mode = stat.S_IMODE(state_dir.stat().st_mode)
        assert file_mode == 0o600
        assert dir_mode == 0o700

        out = capsys.readouterr().out
        assert "cc-memory" in out

    def test_no_leftover_temp_files(self, monkeypatch, state_dir):
        monkeypatch.setattr("sys.stdin", io.StringIO(VALID_URL + "\n"))
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, *, json, timeout: httpx.Response(
                200,
                json={"bearer_token": "bt_secret", "identity": "cc-memory", "expires_at": None},
            ),
        )

        redeem.main()

        entries = list(state_dir.iterdir())
        assert entries == [state_dir / redeem.CREDENTIAL_FILENAME]


class TestMainRejectsMalformedUrl:
    def test_malformed_url_nonzero_exit_no_file_written(self, monkeypatch, capsys, state_dir):
        monkeypatch.setattr("sys.stdin", io.StringIO("not a url at all\n"))

        exit_code = redeem.main()

        assert exit_code != 0
        assert not (state_dir / redeem.CREDENTIAL_FILENAME).exists()
        assert capsys.readouterr().err

    def test_empty_stdin_nonzero_exit(self, monkeypatch, capsys, state_dir):
        monkeypatch.setattr("sys.stdin", io.StringIO(""))

        exit_code = redeem.main()

        assert exit_code != 0
        assert capsys.readouterr().err


class TestMainHttpErrors:
    def test_non_200_response_reports_code_and_message(self, monkeypatch, capsys, state_dir):
        monkeypatch.setattr("sys.stdin", io.StringIO(VALID_URL + "\n"))
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, *, json, timeout: httpx.Response(
                404,
                json={"code": "InviteNotFoundError", "message": "unknown or expired invite"},
            ),
        )

        exit_code = redeem.main()

        assert exit_code != 0
        err = capsys.readouterr().err
        assert "InviteNotFoundError" in err
        assert "unknown or expired invite" in err
        assert not (state_dir / redeem.CREDENTIAL_FILENAME).exists()

    def test_transport_error_reports_relay_liveness_hint(self, monkeypatch, capsys, state_dir):
        monkeypatch.setattr("sys.stdin", io.StringIO(VALID_URL + "\n"))

        def raise_connect_error(url, *, json, timeout):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", raise_connect_error)

        exit_code = redeem.main()

        assert exit_code != 0
        err = capsys.readouterr().err
        assert "起動しているか確認" in err
        assert not (state_dir / redeem.CREDENTIAL_FILENAME).exists()


class TestWriteFailureReportsBearer:
    def test_write_failure_prints_bearer_to_stderr(self, monkeypatch, capsys, state_dir):
        monkeypatch.setattr("sys.stdin", io.StringIO(VALID_URL + "\n"))
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, *, json, timeout: httpx.Response(
                200,
                json={"bearer_token": "bt_orphan", "identity": "cc-memory", "expires_at": None},
            ),
        )

        def raise_replace(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("src.services.relay.redeem.os.replace", raise_replace)

        exit_code = redeem.main()

        assert exit_code != 0
        err = capsys.readouterr().err
        assert "bt_orphan" in err
        assert "revoke" in err
