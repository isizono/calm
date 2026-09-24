"""scripts/rules_promotion_scan.py のテスト。

read-only（書き込みクエリを発行しない）であることと、検知対象ユニットの集計・
embedding未接続時のdegraded応答を検証する。
"""
import sqlite3

import pytest

import src.services.embedding_service as emb
from src.services.topic_service import add_topic
from src.services.tag_service import update_tag

from scripts.rules_promotion_scan import (
    _embed_texts,
    _open_readonly_connection,
    collect_units,
    main,
    render_text_report,
    scan_candidates,
)


@pytest.fixture
def disable_embedding(monkeypatch):
    """embeddingサーバーを常に未接続扱いにする（外部境界のmock）。"""
    monkeypatch.setattr(emb, "_server_initialized", False)
    monkeypatch.setattr(emb, "_backfill_done", True)
    monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)


@pytest.fixture
def enable_embedding_with_spy(monkeypatch):
    """embeddingサーバーが使える状態を模擬し、_encode_batchへの呼び出し(チャンク)を記録する。

    実HTTP通信は行わず、呼び出しごとのテキスト件数だけを観測するspyラッパー
    （テスト対象の内部関数を差し替えるが、実処理は行わずダミーembeddingを返すだけ）。
    """
    monkeypatch.setattr(emb, "_ensure_initialized", lambda: True)

    calls: list[int] = []

    def _fake_encode_batch(texts, prefix):
        calls.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(emb, "_encode_batch", _fake_encode_batch)
    return calls


def _seed_tag(tag_str: str, notes: str) -> None:
    add_topic(title="scanテスト", description="テスト用", tags=[tag_str])
    result = update_tag(tag_str, notes)
    assert "error" not in result


class TestReadOnlyConnection:
    def test_readonly_connection_rejects_writes(self, temp_db):
        conn = _open_readonly_connection(temp_db)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE tags SET notes = ? WHERE id = 1", ("x",))
        finally:
            conn.close()


class TestCollectUnits:
    def test_counts_units_excluding_intent_and_archived(self, temp_db):
        _seed_tag("domain:test", "## 教訓A\n本文A\n")
        _seed_tag("hooks", "前文だけの教訓\n")
        _seed_tag("intent:design", "作業フェーズの行動指示\n")

        conn = _open_readonly_connection(temp_db)
        try:
            units = collect_units(conn)
        finally:
            conn.close()

        tags_seen = {u["tag"] for u in units}
        assert "domain:test" in tags_seen
        assert "hooks" in tags_seen
        assert "intent:design" not in tags_seen  # intent名前空間は検知対象外
        assert len(units) == 2


class TestEmbedTextsChunking:
    def test_splits_into_multiple_encode_batch_calls_over_item_limit(
        self, enable_embedding_with_spy
    ):
        calls = enable_embedding_with_spy
        texts = [f"text{i}" for i in range(emb.BACKFILL_MAX_ITEMS + 5)]

        embeddings = _embed_texts(texts)

        assert embeddings is not None
        assert len(embeddings) == len(texts)
        # 1回のリクエストに収まらない件数を渡すと _encode_batch が複数回呼ばれる
        assert len(calls) >= 2
        assert sum(calls) == len(texts)

    def test_single_chunk_call_returns_embeddings_in_original_order(self, monkeypatch):
        monkeypatch.setattr(emb, "_ensure_initialized", lambda: True)
        monkeypatch.setattr(
            emb,
            "_encode_batch",
            lambda texts, prefix: [[float(i), 0.0] for i in range(len(texts))],
        )

        embeddings = _embed_texts(["a", "b", "c"])
        assert embeddings == [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]]

    def test_failed_chunk_makes_whole_result_none(self, monkeypatch):
        monkeypatch.setattr(emb, "_ensure_initialized", lambda: True)
        monkeypatch.setattr(emb, "_encode_batch", lambda texts, prefix: None)

        texts = [f"text{i}" for i in range(emb.BACKFILL_MAX_ITEMS + 5)]
        assert _embed_texts(texts) is None


class TestScanCandidatesDegraded:
    def test_degraded_when_embedding_server_unavailable(self, temp_db, disable_embedding):
        _seed_tag("domain:test", "## 教訓A\n本文A\n")

        conn = _open_readonly_connection(temp_db)
        try:
            report = scan_candidates(conn)
        finally:
            conn.close()

        assert report["degraded"] is True
        assert report["unit_count"] == 1
        assert report["clusters"] == []

    def test_empty_db_is_not_degraded(self, temp_db, disable_embedding):
        conn = _open_readonly_connection(temp_db)
        try:
            report = scan_candidates(conn)
        finally:
            conn.close()

        # 検知対象ユニットが無い場合はembeddingを呼ばないため degraded にならない
        assert report["degraded"] is False
        assert report["unit_count"] == 0
        assert report["clusters"] == []


class TestRenderTextReport:
    def test_degraded_report_mentions_degraded(self):
        text = render_text_report({"degraded": True, "unit_count": 3, "clusters": []})
        assert "degraded" in text

    def test_cutoff_hint_for_few_clusters(self):
        report = {
            "degraded": False,
            "unit_count": 2,
            "clusters": [
                {
                    "tags": ["domain:test", "hooks"],
                    "units": [
                        {"tag": "domain:test", "heading": "## A", "text": "本文A\n"},
                        {"tag": "hooks", "heading": None, "text": "本文A'\n"},
                    ],
                    "pairs": [],
                }
            ],
        }
        text = render_text_report(report)
        assert "候補1" in text
        assert "domain:test" in text
        assert "打ち切りを検討可" in text


class TestCliMain:
    def test_json_format_output_on_empty_db(self, temp_db, disable_embedding, capsys):
        exit_code = main(["--db-path", temp_db, "--format", "json"])
        assert exit_code == 0

        import json

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["unit_count"] == 0
        assert payload["clusters"] == []
