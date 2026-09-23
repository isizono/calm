"""src/env_compat.py のユニットテスト

環境変数を CALM_ 接頭辞へ統一するにあたり、旧名（CCM_ / CC_MEMORY_）を渡している
既存デプロイを落とさないためのフォールバック解決を検証する。
"""
import os

import pytest

from src.env_compat import (
    env_get,
    env_names,
    env_pop,
    env_restore,
    env_set,
    env_snapshot,
)


class TestEnvNames:
    def test_returns_canonical_then_legacy_names_in_order(self):
        assert env_names("CALM_DB_PATH") == (
            "CALM_DB_PATH",
            "CCM_DB_PATH",
            "CC_MEMORY_DB_PATH",
        )

    def test_rejects_name_without_calm_prefix(self):
        with pytest.raises(ValueError):
            env_names("CCM_DB_PATH")

    def test_rejects_bare_suffix(self):
        with pytest.raises(ValueError):
            env_names("DB_PATH")


class TestEnvGet:
    @pytest.fixture(autouse=True)
    def _clear(self, monkeypatch):
        for name in env_names("CALM_DB_PATH"):
            monkeypatch.delenv(name, raising=False)

    def test_returns_default_when_all_names_unset(self):
        assert env_get("CALM_DB_PATH", "fallback") == "fallback"

    def test_returns_none_when_all_names_unset_and_no_default(self):
        assert env_get("CALM_DB_PATH") is None

    def test_reads_canonical_name(self, monkeypatch):
        monkeypatch.setenv("CALM_DB_PATH", "/new")
        assert env_get("CALM_DB_PATH") == "/new"

    def test_falls_back_to_ccm_name(self, monkeypatch):
        monkeypatch.setenv("CCM_DB_PATH", "/ccm")
        assert env_get("CALM_DB_PATH") == "/ccm"

    def test_falls_back_to_cc_memory_name(self, monkeypatch):
        monkeypatch.setenv("CC_MEMORY_DB_PATH", "/cc-memory")
        assert env_get("CALM_DB_PATH") == "/cc-memory"

    def test_canonical_name_wins_over_both_legacy_names(self, monkeypatch):
        monkeypatch.setenv("CALM_DB_PATH", "/new")
        monkeypatch.setenv("CCM_DB_PATH", "/ccm")
        monkeypatch.setenv("CC_MEMORY_DB_PATH", "/cc-memory")
        assert env_get("CALM_DB_PATH") == "/new"

    def test_ccm_name_wins_over_cc_memory_name(self, monkeypatch):
        monkeypatch.setenv("CCM_DB_PATH", "/ccm")
        monkeypatch.setenv("CC_MEMORY_DB_PATH", "/cc-memory")
        assert env_get("CALM_DB_PATH") == "/ccm"

    def test_empty_string_counts_as_set_and_stops_fallback(self, monkeypatch):
        """空文字も「設定済み」として扱い、旧名へは落ちない。

        os.environ.get と同じ「存在するなら値をそのまま返す」意味論に揃える。
        空文字を未設定として扱う正規化は、必要な呼び出し側（CALM_SYNC_POLICY 等）が
        自分で行う。
        """
        monkeypatch.setenv("CALM_DB_PATH", "")
        monkeypatch.setenv("CCM_DB_PATH", "/ccm")
        assert env_get("CALM_DB_PATH", "fallback") == ""


class TestEnvPop:
    def test_removes_canonical_and_all_legacy_names(self, monkeypatch):
        monkeypatch.setenv("CALM_DB_PATH", "/new")
        monkeypatch.setenv("CCM_DB_PATH", "/ccm")
        monkeypatch.setenv("CC_MEMORY_DB_PATH", "/cc-memory")

        env_pop("CALM_DB_PATH")

        for name in env_names("CALM_DB_PATH"):
            assert name not in os.environ
        assert env_get("CALM_DB_PATH") is None

    def test_is_noop_when_nothing_is_set(self, monkeypatch):
        for name in env_names("CALM_DB_PATH"):
            monkeypatch.delenv(name, raising=False)
        env_pop("CALM_DB_PATH")
        assert env_get("CALM_DB_PATH") is None


class TestEnvSet:
    def test_sets_canonical_name_and_drops_legacy_names(self, monkeypatch):
        monkeypatch.setenv("CCM_DB_PATH", "/ccm")
        monkeypatch.setenv("CC_MEMORY_DB_PATH", "/cc-memory")

        env_set("CALM_DB_PATH", "/new")

        assert os.environ["CALM_DB_PATH"] == "/new"
        assert "CCM_DB_PATH" not in os.environ
        assert "CC_MEMORY_DB_PATH" not in os.environ

    def test_value_does_not_resurrect_from_legacy_name_after_pop(self, monkeypatch):
        """env_set → os.environ.pop(新名) の順でも旧名の値は復活しない。

        旧名を残したまま新名だけ設定すると、新名を pop した時点で旧名の値が
        見えるようになる。env_set はこれを防ぐために旧名ごと畳む。
        """
        monkeypatch.setenv("CCM_DB_PATH", "/ccm")
        env_set("CALM_DB_PATH", "/new")
        os.environ.pop("CALM_DB_PATH", None)
        assert env_get("CALM_DB_PATH") is None


class TestEnvSnapshotRestore:
    def test_restores_all_names_to_their_previous_values(self, monkeypatch):
        monkeypatch.setenv("CCM_DB_PATH", "/ccm")
        monkeypatch.delenv("CALM_DB_PATH", raising=False)
        monkeypatch.delenv("CC_MEMORY_DB_PATH", raising=False)

        saved = env_snapshot("CALM_DB_PATH")
        env_set("CALM_DB_PATH", "/tmp/override")
        assert env_get("CALM_DB_PATH") == "/tmp/override"

        env_restore(saved)

        assert "CALM_DB_PATH" not in os.environ
        assert os.environ["CCM_DB_PATH"] == "/ccm"
        assert "CC_MEMORY_DB_PATH" not in os.environ
        assert env_get("CALM_DB_PATH") == "/ccm"

    def test_restores_unset_state(self, monkeypatch):
        for name in env_names("CALM_DB_PATH"):
            monkeypatch.delenv(name, raising=False)

        saved = env_snapshot("CALM_DB_PATH")
        env_set("CALM_DB_PATH", "/tmp/override")
        env_restore(saved)

        for name in env_names("CALM_DB_PATH"):
            assert name not in os.environ

    def test_restore_accepts_extra_unprefixed_names(self, monkeypatch):
        """dump_db_schema.py のように DISCUSSION_DB_PATH を相乗りさせる使い方。"""
        monkeypatch.setenv("DISCUSSION_DB_PATH", "/discussion")
        saved = {"DISCUSSION_DB_PATH": os.environ.get("DISCUSSION_DB_PATH")}
        saved.update(env_snapshot("CALM_DB_PATH"))

        os.environ["DISCUSSION_DB_PATH"] = "/tmp/other"
        env_restore(saved)

        assert os.environ["DISCUSSION_DB_PATH"] == "/discussion"


class TestDbPathUnification:
    """CALM_DB_PATH への統一で、hook 側の旧名からも同じDBを解決できることを検証。

    改名前は同じ「DBのパス」をサーバー本体が CCM_DB_PATH、hook 群が
    CC_MEMORY_DB_PATH という別名で読んでおり、片方だけ設定すると両者が別DBを
    見る不整合があった。統一後はどちらの旧名からも同一の値に解決される。
    """

    @pytest.fixture(autouse=True)
    def _no_config_db_path(self, monkeypatch):
        import src.config

        monkeypatch.setattr(src.config, "DB_PATH", None)
        monkeypatch.delenv("DISCUSSION_DB_PATH", raising=False)
        for name in env_names("CALM_DB_PATH"):
            monkeypatch.delenv(name, raising=False)

    @pytest.mark.parametrize(
        "name", ["CALM_DB_PATH", "CCM_DB_PATH", "CC_MEMORY_DB_PATH"]
    )
    def test_get_db_path_resolves_new_and_legacy_names(self, monkeypatch, name):
        from src.db import get_db_path

        monkeypatch.setenv(name, "/tmp/calm-test.db")
        assert get_db_path() == "/tmp/calm-test.db"

    def test_get_db_path_prefers_calm_name_over_legacy_hook_name(self, monkeypatch):
        from src.db import get_db_path

        monkeypatch.setenv("CALM_DB_PATH", "/tmp/new.db")
        monkeypatch.setenv("CC_MEMORY_DB_PATH", "/tmp/legacy.db")
        assert get_db_path() == "/tmp/new.db"
