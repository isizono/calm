"""subscription declaration file（src/services/relay/declarations.py）の unit test。"""
import json

import pytest

from src.services.relay import declarations


@pytest.fixture(autouse=True)
def relay_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    return tmp_path / "relay-state"


class TestEnsure:
    def test_creates_declaration_file_with_handle(self, relay_state):
        decl = declarations.ensure("0a1b2c3d-4e5f-6789-abcd-ef0123456789")
        path = declarations.declaration_path("0a1b2c3d-4e5f-6789-abcd-ef0123456789")
        assert path.exists()
        assert decl["handle"] == "session-0a1b2c3d"
        assert decl["subscriptions"] == []
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["handle"] == decl["handle"]

    def test_handle_is_stable_across_calls(self, relay_state):
        first = declarations.ensure("abcd1234-xyz")
        second = declarations.ensure("abcd1234-xyz")
        assert first["handle"] == second["handle"]

    def test_recreates_when_file_is_corrupt(self, relay_state):
        path = declarations.declaration_path("sess-corrupt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        decl = declarations.ensure("sess-corrupt")
        assert decl["handle"].startswith("session-")


class TestLoadSave:
    def test_load_missing_returns_none(self, relay_state):
        assert declarations.load("no-such-session") is None

    def test_roundtrip(self, relay_state):
        decl = {
            "session_id": "sess-1",
            "handle": "session-sess1",
            "subscriptions": [
                {
                    "subscription_id": "sub-1",
                    "labels": ["handle:session-sess1"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                }
            ],
        }
        declarations.save(decl)
        loaded = declarations.load("sess-1")
        assert loaded == decl

    def test_session_id_is_sanitized_for_filename(self, relay_state):
        decl = declarations.ensure("weird/../id")
        path = declarations.declaration_path("weird/../id")
        assert path.parent == declarations.config.subscriptions_dir()
        assert path.exists()
        assert decl["session_id"] == "weird/../id"


class TestListDeclaredSessionIds:
    def test_returns_empty_set_when_dir_missing(self, relay_state):
        assert declarations.list_declared_session_ids() == set()

    def test_returns_safe_session_ids_from_filenames(self, relay_state):
        declarations.ensure("sess-1")
        declarations.ensure("sess-2")
        assert declarations.list_declared_session_ids() == {"sess-1", "sess-2"}

    def test_ignores_non_declaration_files(self, relay_state, tmp_path):
        declarations.ensure("sess-1")
        (declarations.config.subscriptions_dir() / "not-a-declaration.txt").write_text("x")
        assert declarations.list_declared_session_ids() == {"sess-1"}

    def test_does_not_require_reading_file_contents(self, relay_state):
        """declaration_path() が指すfileが壊れたJSONでも、ファイル名一覧としては拾える
        （中身は読まない軽量版のため、load_all()と違い破損に影響されない）。"""
        path = declarations.declaration_path("sess-corrupt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert declarations.list_declared_session_ids() == {"sess-corrupt"}


class TestSubscriptionLookup:
    def _decl(self):
        return {
            "session_id": "s",
            "handle": "session-s",
            "subscriptions": [
                {
                    "subscription_id": "sub-1",
                    "labels": ["a", "b"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                }
            ],
        }

    def test_find_matches_as_set(self):
        decl = self._decl()
        assert declarations.find_subscription(decl, ["b", "a"]) is not None
        assert declarations.find_subscription(decl, ["a", "b", "a"]) is not None

    def test_find_returns_none_for_different_set(self):
        decl = self._decl()
        assert declarations.find_subscription(decl, ["a"]) is None
        assert declarations.find_subscription(decl, ["a", "b", "c"]) is None

    def test_upsert_replaces_same_labels_entry(self):
        decl = self._decl()
        declarations.upsert_subscription(
            decl,
            {
                "subscription_id": "sub-2",
                "labels": ["b", "a"],
                "lease_expires_at": "2099-06-01T00:00:00Z",
                "created_at": "2026-07-05T01:00:00Z",
            },
        )
        assert len(decl["subscriptions"]) == 1
        assert decl["subscriptions"][0]["subscription_id"] == "sub-2"

    def test_upsert_appends_new_labels_entry(self):
        decl = self._decl()
        declarations.upsert_subscription(
            decl,
            {
                "subscription_id": "sub-3",
                "labels": ["c"],
                "lease_expires_at": "2099-06-01T00:00:00Z",
                "created_at": "2026-07-05T01:00:00Z",
            },
        )
        assert len(decl["subscriptions"]) == 2


class TestLeaseActive:
    def test_future_lease_is_active(self):
        assert declarations.lease_active({"lease_expires_at": "2099-01-01T00:00:00Z"})

    def test_past_lease_is_inactive(self):
        assert not declarations.lease_active(
            {"lease_expires_at": "2020-01-01T00:00:00Z"}
        )

    def test_missing_or_garbage_is_inactive(self):
        assert not declarations.lease_active({})
        assert not declarations.lease_active({"lease_expires_at": None})
        assert not declarations.lease_active({"lease_expires_at": "not-a-date"})

    def test_offset_format_is_supported(self):
        assert declarations.lease_active(
            {"lease_expires_at": "2099-01-01T00:00:00+00:00"}
        )
