"""subscription declaration file（src/services/relay/declarations.py）の unit test。"""
import json
from datetime import datetime, timezone

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


class TestNormalizeAllDeclarations:
    """旧形式（話題labels + 自handle混入）のdeclarationを正規化する
    normalize_all_declarations()のunit test。relay_subscribeのhandle自動付与廃止に
    伴う移行処理本体。"""

    def _write(self, session_id: str, handle: str, subs: list[dict]) -> None:
        decl = {"session_id": session_id, "handle": handle, "subscriptions": subs}
        declarations.save(decl)

    def test_strips_own_handle_from_mixed_entry_and_expires_lease(self, relay_state):
        self._write(
            "sess-1",
            "session-abc",
            [
                {
                    "subscription_id": "sub-1",
                    "labels": ["room:planning", "handle:session-abc"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                }
            ],
        )

        changed = declarations.normalize_all_declarations()

        assert changed == 1
        decl = declarations.load("sess-1")
        entry = decl["subscriptions"][0]
        assert entry["labels"] == ["room:planning"]
        # lease_expires_atは削除されず、現在時刻付近（失効直後扱い）に更新される
        # （孤児sweepの即死条件「lease_expires_at欠落=無限に古い扱い」を踏まないため）。
        expires = datetime.fromisoformat(entry["lease_expires_at"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        assert abs((now - expires).total_seconds()) < 10

    def test_handle_only_entry_is_left_untouched(self, relay_state):
        """自handle単独のentry（直接メッセージ購読）は正規化対象外。"""
        self._write(
            "sess-1",
            "session-abc",
            [
                {
                    "subscription_id": "sub-1",
                    "labels": ["handle:session-abc"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                }
            ],
        )

        changed = declarations.normalize_all_declarations()

        assert changed == 0
        decl = declarations.load("sess-1")
        assert decl["subscriptions"][0]["labels"] == ["handle:session-abc"]
        assert decl["subscriptions"][0]["lease_expires_at"] == "2099-01-01T00:00:00Z"

    def test_other_sessions_handle_in_composite_entry_is_left_untouched(self, relay_state):
        """自分以外のhandleを含む複合entryは意図的な指定でありうるため触らない。"""
        self._write(
            "sess-1",
            "session-abc",
            [
                {
                    "subscription_id": "sub-1",
                    "labels": ["room:planning", "handle:session-other"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                }
            ],
        )

        changed = declarations.normalize_all_declarations()

        assert changed == 0
        decl = declarations.load("sess-1")
        assert set(decl["subscriptions"][0]["labels"]) == {
            "room:planning", "handle:session-other",
        }

    def test_entry_with_handle_auto_attached_marker_is_never_touched(self, relay_state):
        """`handle_auto_attached`キーを持つentry（handle自動付与廃止後のコードが
        作成した = relay_subscribeが常に付与する）は、自handle混入に見える形で
        あっても絶対に正規化しない。「宛先を自分に限定した複合条件をlabelsに
        自分のhandle labelを明示的に含める」という推奨される意図的な使い方を、
        移行処理が旧バグの残骸と誤認して破壊しないための保護。"""
        self._write(
            "sess-1",
            "session-abc",
            [
                {
                    "subscription_id": "sub-1",
                    "labels": ["room:planning", "handle:session-abc"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                    "handle_auto_attached": False,
                }
            ],
        )

        changed = declarations.normalize_all_declarations()

        assert changed == 0
        decl = declarations.load("sess-1")
        assert set(decl["subscriptions"][0]["labels"]) == {
            "room:planning", "handle:session-abc",
        }
        assert decl["subscriptions"][0]["lease_expires_at"] == "2099-01-01T00:00:00Z"

    def test_marked_entry_and_legacy_entry_can_coexist_without_collision(self, relay_state):
        """マーカー付きentryとマーカー無しentryのlabels集合が異なれば、
        正規化はマーカー無し側だけを独立にstripする（衝突しない）。"""
        self._write(
            "sess-1",
            "session-abc",
            [
                {
                    "subscription_id": "sub-marked",
                    "labels": ["room:planning", "handle:session-abc"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                    "handle_auto_attached": False,
                },
                {
                    "subscription_id": "sub-legacy",
                    "labels": ["task:build", "handle:session-abc"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                },
            ],
        )

        changed = declarations.normalize_all_declarations()

        assert changed == 1
        decl = declarations.load("sess-1")
        assert len(decl["subscriptions"]) == 2
        by_id = {e["subscription_id"]: e for e in decl["subscriptions"]}
        assert set(by_id["sub-marked"]["labels"]) == {"room:planning", "handle:session-abc"}
        assert by_id["sub-legacy"]["labels"] == ["task:build"]

    def test_normalization_dedupes_entries_that_collapse_to_same_labels(self, relay_state):
        """正規化でhandleを外した結果、別entryと同じlabels集合になった場合は片方を落とす。

        衝突した2entryのうち、今回strip対象になった側（sub-1、旧形式の残骸）ではなく、
        元から健全だった側（sub-2、活きたleaseを持つ既存subscription）が生き残ること
        を検証する。出現順（sub-1が先）に引きずられて健全な方を誤って落とすと、
        renewされ続けてきた生きたsubscriptionを失う。
        """
        self._write(
            "sess-1",
            "session-abc",
            [
                {
                    "subscription_id": "sub-1",
                    "labels": ["room:planning", "handle:session-abc"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                },
                {
                    "subscription_id": "sub-2",
                    "labels": ["room:planning"],
                    "lease_expires_at": "2099-06-01T00:00:00Z",
                    "created_at": "2026-07-05T01:00:00Z",
                },
            ],
        )

        changed = declarations.normalize_all_declarations()

        assert changed == 1
        decl = declarations.load("sess-1")
        assert len(decl["subscriptions"]) == 1
        survivor = decl["subscriptions"][0]
        assert survivor["subscription_id"] == "sub-2"
        assert survivor["lease_expires_at"] == "2099-06-01T00:00:00Z"

    def test_normalization_dedup_prefers_later_lease_when_both_stripped(self, relay_state):
        """衝突した2entryが両方ともstrip対象（両方とも旧形式）の場合は、
        lease_expires_atがより未来（＝より最近renewされた）側を残す。"""
        self._write(
            "sess-1",
            "session-abc",
            [
                {
                    "subscription_id": "sub-old",
                    "labels": ["room:planning", "handle:session-abc"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                },
                {
                    "subscription_id": "sub-newer",
                    "labels": ["room:planning", "handle:session-abc"],
                    "lease_expires_at": "2099-06-01T00:00:00Z",
                    "created_at": "2026-07-05T01:00:00Z",
                },
            ],
        )

        changed = declarations.normalize_all_declarations()

        assert changed == 1
        decl = declarations.load("sess-1")
        assert len(decl["subscriptions"]) == 1
        survivor = decl["subscriptions"][0]
        assert survivor["subscription_id"] == "sub-newer"
        assert survivor["labels"] == ["room:planning"]

    def test_idempotent_second_run_is_noop(self, relay_state):
        self._write(
            "sess-1",
            "session-abc",
            [
                {
                    "subscription_id": "sub-1",
                    "labels": ["room:planning", "handle:session-abc"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                }
            ],
        )

        first = declarations.normalize_all_declarations()
        second = declarations.normalize_all_declarations()

        assert first == 1
        assert second == 0

    def test_normalized_entry_is_classified_as_resubscribe_by_lease_loop(self, relay_state):
        """正規化直後のentryは、lease_loopのcompute_renew_actionsから見ると
        「期限切れ→resubscribe」に分類される。これがlease_expires_atを現在時刻に
        更新する目的そのもの（削除ではなく更新にすることで、この判定に確実に乗せて
        新labelsでの再購読につなげる）。"""
        from src.services.relay.lease_loop import compute_renew_actions

        self._write(
            "sess-1",
            "session-abc",
            [
                {
                    "subscription_id": "sub-1",
                    "labels": ["room:planning", "handle:session-abc"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                }
            ],
        )
        declarations.normalize_all_declarations()

        snapshot = declarations.load_all()
        actions = compute_renew_actions(snapshot, active_session_ids={"sess-1"})

        assert len(actions) == 1
        assert actions[0].kind == "resubscribe"
        assert actions[0].session_id == "sess-1"
        assert actions[0].labels == ["room:planning"]

    def test_normalized_entry_survives_orphan_sweep_threshold(self, relay_state):
        """正規化直後のlease_expires_at（現在時刻）は孤児sweepの24時間閾値に絶対に
        掛からない（lease_expires_atを削除する誤実装だと、期限不明=無限に古い扱いで
        即sweepされてしまう）。"""
        from src.services.relay.lease_loop import compute_orphan_sessions

        self._write(
            "sess-1",
            "session-abc",
            [
                {
                    "subscription_id": "sub-1",
                    "labels": ["room:planning", "handle:session-abc"],
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "created_at": "2026-07-05T00:00:00Z",
                }
            ],
        )
        declarations.normalize_all_declarations()

        snapshot = declarations.load_all()
        orphans = compute_orphan_sessions(snapshot)
        assert "sess-1" not in orphans

    def test_no_declarations_returns_zero(self, relay_state):
        assert declarations.normalize_all_declarations() == 0

    def test_missing_handle_key_does_not_raise(self, relay_state):
        """handleキー欠落（壊れたdeclaration）でも例外を出さずスキップする。"""
        self._write("sess-1", "", [
            {
                "subscription_id": "sub-1",
                "labels": ["room:planning"],
                "lease_expires_at": "2099-01-01T00:00:00Z",
                "created_at": "2026-07-05T00:00:00Z",
            }
        ])
        assert declarations.normalize_all_declarations() == 0


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
