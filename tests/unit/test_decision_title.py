"""decision title + 関連decision返却 のテスト（migration 0037 で追加した機能）

- add_decisions が optional title を受け取り decisions.title に保存する
- add_decisions のレスポンス created各要素に related_decisions（同topic内の類似decision）が付く
- find_similar_decisions が同一topic・自身除外・retract除外で類似decisionを返す
- 表示箇所（check-in の recent_decisions / get_by_id）が title優先・decision本文fallback になる
"""
import os
import tempfile

import numpy as np
import pytest

from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.decision_service import add_decisions
from src.services.search_service import find_similar_decisions, get_by_id
from src.services.checkin_service import _get_decisions_from_topics
from src.services.tag_service import _injected_tags
import src.services.embedding_service as emb


EMBEDDING_DIM = 384
DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def topic(temp_db):
    return add_topic(title="テストトピック", description="テスト用", tags=DEFAULT_TAGS)


@pytest.fixture
def topic2(temp_db):
    return add_topic(title="テストトピック2", description="テスト用2", tags=["domain:test", "extra"])


@pytest.fixture
def mock_embedding_server(monkeypatch):
    """embedding_serverへのHTTPリクエストをモック化（テキストごとに決定的なベクトルを返す）"""
    def mock_encode_batch(texts, prefix):
        embeddings = []
        for text in texts:
            prefix_str = "検索文書: " if prefix == "document" else "検索クエリ: "
            np.random.seed(hash(prefix_str + text) % (2**32))
            embeddings.append(np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist())
        return embeddings

    monkeypatch.setattr(emb, '_encode_batch', mock_encode_batch)
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)
    yield


@pytest.fixture
def mock_embedding_unavailable(monkeypatch):
    """embeddingサーバーが利用不可（encode系がNoneを返す）状態をモック化する。

    実環境でembedding_serverが起動しているか否かに依存せず、
    embedding取得失敗時の挙動を決定的に検証するために使う。
    """
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_encode_batch', lambda texts, prefix: None)
    yield


def _decision_title_in_db(decision_id: int):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT title FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        return row["title"] if row else None
    finally:
        conn.close()


class TestTitleStored:
    """add_decisions が title を保存する"""

    def test_title_persisted_when_provided(self, topic):
        """titleを指定するとdecisions.titleに保存される"""
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "本文", "reason": "理由", "title": "要点1行"},
        ])
        assert "error" not in result
        did = result["created"][0]["decision_id"]
        assert _decision_title_in_db(did) == "要点1行"

    def test_title_null_when_omitted(self, topic):
        """title省略時はdecisions.titleがNULLになる"""
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "本文", "reason": "理由"},
        ])
        assert "error" not in result
        did = result["created"][0]["decision_id"]
        assert _decision_title_in_db(did) is None

    def test_empty_or_whitespace_title_normalized_to_null(self, topic):
        """空文字・空白のみのtitleはNULLに正規化される（表示fallbackを全箇所で一致させるため）"""
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "本文1", "reason": "理由", "title": ""},
            {"topic_id": topic["topic_id"], "decision": "本文2", "reason": "理由", "title": "   "},
        ])
        assert "error" not in result
        for c in result["created"]:
            assert _decision_title_in_db(c["decision_id"]) is None


class TestRelatedDecisionsResponse:
    """add_decisions のレスポンスに related_decisions が付く"""

    def test_created_has_related_decisions_key(self, topic, mock_embedding_server):
        """created各要素に related_decisions キーが存在する"""
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "決定A", "reason": "理由A"},
        ])
        assert "error" not in result
        assert "related_decisions" in result["created"][0]
        assert isinstance(result["created"][0]["related_decisions"], list)

    def test_related_includes_prior_same_topic_decision(self, topic, mock_embedding_server):
        """同topicの既存decisionが related_decisions に含まれ、自身は除外される"""
        first = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "既存決定", "reason": "理由", "title": "既存の要点"},
        ])
        first_id = first["created"][0]["decision_id"]

        second = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "新しい決定", "reason": "理由"},
        ])
        second_id = second["created"][0]["decision_id"]
        related = second["created"][0]["related_decisions"]

        related_ids = [r["id"] for r in related]
        assert first_id in related_ids, "同topicの既存decisionがrelated_decisionsに含まれない"
        assert second_id not in related_ids, "自身がrelated_decisionsに含まれている"

    def test_related_title_uses_fallback(self, topic, mock_embedding_server):
        """related_decisions の title は title優先・decision本文fallback"""
        # titleありの既存decision
        add_decisions([
            {"topic_id": topic["topic_id"], "decision": "本文A", "reason": "理由", "title": "要点A"},
        ])
        # titleなしの既存decision
        add_decisions([
            {"topic_id": topic["topic_id"], "decision": "本文B（titleなし）", "reason": "理由"},
        ])
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "トリガー決定", "reason": "理由"},
        ])
        related = {r["title"] for r in result["created"][0]["related_decisions"]}
        assert "要点A" in related, "titleありdecisionがtitleで表示されていない"
        assert "本文B（titleなし）" in related, "titleなしdecisionがdecision本文にfallbackしていない"

    def test_related_excludes_other_topic(self, topic, topic2, mock_embedding_server):
        """別topicのdecisionは related_decisions に含まれない"""
        other = add_decisions([
            {"topic_id": topic2["topic_id"], "decision": "別topic決定", "reason": "理由"},
        ])
        other_id = other["created"][0]["decision_id"]

        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "対象topic決定", "reason": "理由"},
        ])
        related_ids = [r["id"] for r in result["created"][0]["related_decisions"]]
        assert other_id not in related_ids, "別topicのdecisionがrelated_decisionsに混入している"

    def test_related_within_batch_respects_processing_order(self, topic, mock_embedding_server):
        """同一バッチ内では、後続decisionのrelatedに先行decisionが現れ、逆は現れない。

        add_decisionsはcreated順に「embedding生成→find_similar_decisions」を行う。
        後続要素のfind時点では先行要素のembeddingが既にvec_index格納済みだが、
        先行要素のfind時点では後続要素のembeddingが未格納であることに由来する挙動。
        """
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "バッチ先行", "reason": "理由"},
            {"topic_id": topic["topic_id"], "decision": "バッチ後続", "reason": "理由"},
        ])
        first_id = result["created"][0]["decision_id"]
        second_id = result["created"][1]["decision_id"]

        first_related = [r["id"] for r in result["created"][0]["related_decisions"]]
        second_related = [r["id"] for r in result["created"][1]["related_decisions"]]

        assert first_id in second_related, "後続decisionのrelatedに先行decisionが現れていない"
        assert second_id not in first_related, "先行decisionのrelatedに後続decisionが現れている（処理順序の前提が崩れている）"

    def test_related_capped_at_limit(self, topic, mock_embedding_server):
        """同topicに4件以上の先行decisionがあっても related_decisions は上位3件に絞られる"""
        for i in range(4):
            add_decisions([
                {"topic_id": topic["topic_id"], "decision": f"先行決定{i}", "reason": "理由"},
            ])
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "トリガー決定", "reason": "理由"},
        ])
        related = result["created"][0]["related_decisions"]
        assert len(related) == 3, f"related_decisionsがlimit(3)で絞られていない: {len(related)}件"

    def test_related_empty_when_embedding_unavailable(self, topic, mock_embedding_unavailable):
        """embeddingが取得できない場合は related_decisions が空配列になる"""
        # 既存decisionを1件作成（embeddingは生成されない）
        add_decisions([
            {"topic_id": topic["topic_id"], "decision": "既存決定", "reason": "理由"},
        ])
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "新決定", "reason": "理由"},
        ])
        assert result["created"][0]["related_decisions"] == []


class TestFindSimilarDecisions:
    """find_similar_decisions の単体挙動"""

    def test_excludes_retracted(self, topic, mock_embedding_server):
        """retract済みdecisionは結果に含まれない"""
        retracted = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "撤回される決定", "reason": "理由"},
        ])
        retracted_id = retracted["created"][0]["decision_id"]

        # retractフラグを立てる
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE decisions SET retracted_at = '2026-01-01 00:00:00' WHERE id = ?",
                (retracted_id,),
            )
            conn.commit()
        finally:
            conn.close()

        anchor = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "アンカー決定", "reason": "理由"},
        ])
        anchor_id = anchor["created"][0]["decision_id"]

        results = find_similar_decisions(
            exclude_id=anchor_id,
            topic_id=topic["topic_id"],
            text="アンカー決定",
        )
        result_ids = [r["id"] for r in results]
        assert retracted_id not in result_ids, "retract済みdecisionが結果に含まれている"

    def test_returns_empty_when_embedding_unavailable(self, topic, mock_embedding_unavailable):
        """embeddingが取得できない場合は空リストを返す"""
        add_decisions([
            {"topic_id": topic["topic_id"], "decision": "既存", "reason": "理由"},
        ])
        results = find_similar_decisions(
            exclude_id=99999,
            topic_id=topic["topic_id"],
            text="何か",
        )
        assert results == []


class TestDisplayFallback:
    """表示箇所の title優先・decision本文fallback"""

    def test_checkin_recent_decisions_title_fallback(self, topic):
        """_get_decisions_from_topics が title優先・decision本文fallbackで返す"""
        with_title = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "本文X", "reason": "理由", "title": "要点X"},
        ])
        without_title = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "本文Y（titleなし）", "reason": "理由"},
        ])
        with_id = with_title["created"][0]["decision_id"]
        without_id = without_title["created"][0]["decision_id"]

        conn = get_connection()
        try:
            decisions = _get_decisions_from_topics(conn, [topic["topic_id"]])
        finally:
            conn.close()

        by_id = {d["id_raw"]: d["title"] for d in decisions}
        assert by_id[with_id] == "要点X", "titleありdecisionがtitleで表示されない"
        assert by_id[without_id] == "本文Y（titleなし）", "titleなしdecisionがdecision本文にfallbackしない"

    def test_get_by_id_decision_includes_title(self, topic):
        """get_by_id(decision) のレスポンスに title フィールドが含まれる"""
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "本文", "reason": "理由", "title": "要点Z"},
        ])
        did = result["created"][0]["decision_id"]

        res = get_by_id("decision", did)
        assert "error" not in res
        data = res["data"]
        assert data["title"] == "要点Z"
        # decision本文も従来通り含まれる
        assert data["decision"] == "本文"

    def test_get_by_id_decision_title_fallback_when_omitted(self, topic):
        """title未指定decisionのget_by_idはdecision本文先頭50文字をfallback表示する

        title が None のまま返ると check-in / search の見出しに何も出ないため、
        decision 本文の先頭50文字を title にfallbackする (_get_decisions_from_topics と同じ挙動)。
        """
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "本文だけのdecision", "reason": "理由"},
        ])
        did = result["created"][0]["decision_id"]

        res = get_by_id("decision", did)
        assert res["data"]["title"] == "本文だけのdecision"
        assert res["data"]["id_raw"] == did
        assert "id" not in res["data"]
