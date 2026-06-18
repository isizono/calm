"""ow runtime state ファイルキャッシュ (src/services/ow/cache.py) のユニットテスト.

裁定 L3 (corruption 時は backup なし即削除) を含む 4 フォールバック条件と
正常系 round-trip、OW_STATE_DIR override (tmp_path) を検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services.ow import cache
from src.services.ow.cache import (
    CURRENT_SCHEMA_VERSION,
    OwState,
    load_state,
    save_state,
)


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """各テストで OW_STATE_DIR を tmp_path に向ける (ホーム汚染防止)."""
    monkeypatch.setenv("OW_STATE_DIR", str(tmp_path))
    return tmp_path


def _make_state(channel: str = "P_test", last_msg_id: int = 42) -> OwState:
    return OwState(
        schema_version=CURRENT_SCHEMA_VERSION,
        channel=channel,
        last_msg_id=last_msg_id,
        workers={"w-a": {"state": "working", "last_heartbeat_at": "2026-06-18T11:00:00Z"}},
        identities={"w-a": {"role": "worker", "alias": "w-a"}},
        presence=["w-a", "orch"],
        updated_at="2026-06-18T11:00:00+00:00",
    )


class TestRoundTrip:
    def test_save_then_load_returns_equal_state(self, _isolated_state_dir: Path) -> None:
        """save_state で書いた state を load_state で読み戻すと全フィールドが一致する."""
        state = _make_state(channel="P_round", last_msg_id=123)
        save_state(topic_id=454, state=state)

        loaded = load_state(topic_id=454)

        assert loaded is not None
        assert loaded["schema_version"] == CURRENT_SCHEMA_VERSION
        assert loaded["channel"] == "P_round"
        assert loaded["last_msg_id"] == 123
        assert loaded["workers"] == state["workers"]
        assert loaded["identities"] == state["identities"]
        assert loaded["presence"] == state["presence"]
        assert loaded["updated_at"] == state["updated_at"]

    def test_save_writes_schema_version_as_first_key(self, _isolated_state_dir: Path) -> None:
        """JSON 出力で schema_version が先頭フィールドに来る (裁定 L4)."""
        save_state(topic_id=454, state=_make_state())

        path = _isolated_state_dir / "topic-454.json"
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        assert next(iter(data.keys())) == "schema_version"

    def test_save_fills_updated_at_when_missing(self, _isolated_state_dir: Path) -> None:
        """updated_at が未指定の state でも save_state が現在時刻を埋める."""
        state_without_updated_at: dict = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "channel": "P_test",
            "last_msg_id": 0,
            "workers": {},
            "identities": {},
            "presence": [],
        }
        save_state(topic_id=454, state=state_without_updated_at)  # type: ignore[arg-type]

        loaded = load_state(topic_id=454)
        assert loaded is not None
        assert loaded["updated_at"]  # 何らかの非空 ISO8601 文字列


class TestFallbackCacheMissing:
    """フォールバック条件 1: キャッシュファイルが存在しない."""

    def test_load_returns_none_when_file_absent(self, _isolated_state_dir: Path) -> None:
        """ファイル不存在では None を返し、ファイル作成も削除もしない."""
        result = load_state(topic_id=999)

        assert result is None
        assert not (_isolated_state_dir / "topic-999.json").exists()


class TestFallbackCorruption:
    """フォールバック条件 2: JSON corruption (JSONDecodeError) → ファイル削除して None."""

    def test_load_deletes_corrupt_file_and_returns_none(
        self, _isolated_state_dir: Path
    ) -> None:
        """壊れた JSON は load_state 内で削除され None が返る (裁定 L3: backup なし)."""
        path = _isolated_state_dir / "topic-454.json"
        path.write_text("{ this is not valid json ::: ", encoding="utf-8")

        result = load_state(topic_id=454)

        assert result is None
        assert not path.exists()

    def test_load_deletes_empty_file_and_returns_none(
        self, _isolated_state_dir: Path
    ) -> None:
        """空ファイルも JSONDecodeError 扱いで削除して None を返す."""
        path = _isolated_state_dir / "topic-454.json"
        path.write_text("", encoding="utf-8")

        result = load_state(topic_id=454)

        assert result is None
        assert not path.exists()


class TestFallbackSchemaVersionMismatch:
    """フォールバック条件 3: schema_version mismatch → ファイル削除して None."""

    def test_load_deletes_file_when_schema_version_higher(
        self, _isolated_state_dir: Path
    ) -> None:
        """CURRENT_SCHEMA_VERSION より大きいバージョンの cache は削除される."""
        path = _isolated_state_dir / "topic-454.json"
        path.write_text(
            json.dumps({"schema_version": CURRENT_SCHEMA_VERSION + 99, "channel": "P_x"}),
            encoding="utf-8",
        )

        result = load_state(topic_id=454)

        assert result is None
        assert not path.exists()

    def test_load_deletes_file_when_schema_version_missing(
        self, _isolated_state_dir: Path
    ) -> None:
        """schema_version フィールド自体が無い cache も mismatch 扱いで削除される."""
        path = _isolated_state_dir / "topic-454.json"
        path.write_text(json.dumps({"channel": "P_x", "last_msg_id": 0}), encoding="utf-8")

        result = load_state(topic_id=454)

        assert result is None
        assert not path.exists()


class TestFallbackChannelMismatch:
    """フォールバック条件 4: channel mismatch (引数指定時のみ) → ファイル削除して None."""

    def test_load_deletes_file_on_channel_mismatch(
        self, _isolated_state_dir: Path
    ) -> None:
        """channel 引数と cache 内 channel が異なる場合は削除して None を返す."""
        save_state(topic_id=454, state=_make_state(channel="P_old"))
        path = _isolated_state_dir / "topic-454.json"
        assert path.exists()

        result = load_state(topic_id=454, channel="P_new")

        assert result is None
        assert not path.exists()

    def test_load_accepts_when_channel_matches(self, _isolated_state_dir: Path) -> None:
        """channel 一致時は state を返す."""
        save_state(topic_id=454, state=_make_state(channel="P_same"))

        result = load_state(topic_id=454, channel="P_same")

        assert result is not None
        assert result["channel"] == "P_same"

    def test_load_skips_channel_check_when_arg_none(
        self, _isolated_state_dir: Path
    ) -> None:
        """channel 引数 None なら channel mismatch チェックはスキップされる."""
        save_state(topic_id=454, state=_make_state(channel="P_whatever"))

        result = load_state(topic_id=454, channel=None)

        assert result is not None
        assert result["channel"] == "P_whatever"


class TestOwStateDirOverride:
    """OW_STATE_DIR 環境変数で書き込み先を override できる (裁定 L5)."""

    def test_env_var_directs_file_to_override_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OW_STATE_DIR が指す tmp_path 配下に topic-<id>.json が作られる."""
        override_dir = tmp_path / "custom-cache"
        monkeypatch.setenv("OW_STATE_DIR", str(override_dir))

        save_state(topic_id=777, state=_make_state())

        assert (override_dir / "topic-777.json").exists()

    def test_get_state_dir_falls_back_to_home_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OW_STATE_DIR 未設定時は ~/.cc-memory/ow/cache/ が返る."""
        monkeypatch.delenv("OW_STATE_DIR", raising=False)

        result = cache._get_state_dir()

        assert result == Path.home() / ".cc-memory" / "ow" / "cache"

    def test_get_state_dir_expands_user_in_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OW_STATE_DIR に ~ を含めても expanduser される."""
        monkeypatch.setenv("OW_STATE_DIR", "~/some/cache")

        result = cache._get_state_dir()

        assert result == Path.home() / "some" / "cache"
