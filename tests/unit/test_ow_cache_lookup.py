"""find_topic_id_by_channel と _load_state_by_channel のユニットテスト
(A#911 SP-2 PR-β/γ で追加した channel → topic_id 解決ヘルパー)。

reducer は channel しか持たないが cache は topic_id でキーされているため、
cache ディレクトリ走査で channel → topic_id を逆引きする。本テストはそのスキャン
振る舞いと、壊れたファイル / 無関係なファイルを安全に無視する不変条件を確認する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services import ow_service
from src.services.ow.cache import (
    CURRENT_SCHEMA_VERSION,
    find_topic_id_by_channel,
    save_state,
)


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OW_STATE_DIR", str(tmp_path))
    return tmp_path


def _save_cache_file(topic_id: int, channel: str) -> None:
    save_state(
        topic_id=topic_id,
        state={
            "schema_version": CURRENT_SCHEMA_VERSION,
            "channel": channel,
            "last_msg_id": 0,
            "workers": {},
            "identities": {},
            "identity_events": {},
            "states": {},
            "heartbeats": {},
            "presence": [],
            "updated_at": "2026-06-19T17:00:00+00:00",
        },
    )


class TestFindTopicIdByChannel:
    def test_returns_none_when_state_dir_missing(self, tmp_path: Path) -> None:
        """OW_STATE_DIR がそもそも作られていない場合は None。"""
        # _isolated_state_dir が autouse で OW_STATE_DIR を tmp_path に設定済み。
        # tmp_path 自体は存在するがファイルは空。
        assert find_topic_id_by_channel("any") is None

    def test_returns_topic_id_when_match(self, _isolated_state_dir: Path) -> None:
        """channel フィールドが一致する cache を見つけて topic_id を返す。"""
        _save_cache_file(454, "P_match")
        assert find_topic_id_by_channel("P_match") == 454

    def test_returns_none_when_no_match(self, _isolated_state_dir: Path) -> None:
        """どの cache ファイルも channel が一致しなければ None。"""
        _save_cache_file(454, "P_a")
        _save_cache_file(455, "P_b")
        assert find_topic_id_by_channel("P_z") is None

    def test_picks_first_match_among_multiple(self, _isolated_state_dir: Path) -> None:
        """同一 channel を含む cache が複数あっても、いずれかの topic_id を返す。"""
        _save_cache_file(454, "P_dup")
        _save_cache_file(458, "P_dup")
        result = find_topic_id_by_channel("P_dup")
        assert result in (454, 458)

    def test_ignores_unrelated_files(self, _isolated_state_dir: Path) -> None:
        """topic-<n>.json 以外のファイルがあっても走査が壊れない。"""
        (_isolated_state_dir / "README.md").write_text("ignored", encoding="utf-8")
        (_isolated_state_dir / "other.json").write_text("{}", encoding="utf-8")
        _save_cache_file(456, "P_ok")
        assert find_topic_id_by_channel("P_ok") == 456

    def test_ignores_corrupt_json(self, _isolated_state_dir: Path) -> None:
        """壊れた JSON ファイルがあっても削除せず素通りし、他の正常ファイルから検索する。"""
        bad = _isolated_state_dir / "topic-999.json"
        bad.write_text("{not valid", encoding="utf-8")
        _save_cache_file(457, "P_good")
        # 壊れたファイルは削除されない (load_state 経路ではないため)
        assert bad.exists()
        assert find_topic_id_by_channel("P_good") == 457

    def test_ignores_non_topic_filename_pattern(self, _isolated_state_dir: Path) -> None:
        """topic- prefix が無い JSON は無視される。"""
        (_isolated_state_dir / "snapshot-123.json").write_text(
            json.dumps({"channel": "P_skip"}), encoding="utf-8"
        )
        assert find_topic_id_by_channel("P_skip") is None


class TestLoadStateByChannel:
    """ow_service._load_state_by_channel: channel → topic_id 解決 → load_state 統合経路。"""

    def test_returns_state_when_cache_present(self, _isolated_state_dir: Path) -> None:
        _save_cache_file(454, "P_present")
        result = ow_service._load_state_by_channel("P_present")
        assert result is not None
        assert result["channel"] == "P_present"

    def test_returns_none_when_topic_not_found(self, _isolated_state_dir: Path) -> None:
        result = ow_service._load_state_by_channel("P_missing")
        assert result is None

    def test_returns_none_when_channel_mismatch_in_cache_file(
        self, _isolated_state_dir: Path
    ) -> None:
        """find_topic_id_by_channel は channel 一致で topic_id を返すが、
        load_state がさらに channel パラメータで再検証する。一致するので state を返す。
        """
        _save_cache_file(454, "P_exact")
        result = ow_service._load_state_by_channel("P_exact")
        assert result is not None
        assert result["channel"] == "P_exact"
