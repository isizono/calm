"""retract_service のテスト

エンティティ（decision, log, material）のretract/un-retract操作、
冪等性、部分成功、バリデーションエラーをカバーする。
"""
import os
import tempfile
import numpy as np
import pytest

from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.discussion_log_service import add_logs
from src.services.decision_service import add_decisions
from src.services.material_service import add_material, get_material
from src.services.retract_service import retract
from src.services.search_service import search
from src.services.tag_service import _injected_tags
from src.services.pin_service import add_pin
from src.services.activity_service import add_activity
import src.services.embedding_service as emb


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
    """テスト用トピックを作成する"""
    return add_topic(title="テストトピック", description="テスト用", tags=DEFAULT_TAGS)


EMBEDDING_DIM = 384


@pytest.fixture
def mock_embedding_server(monkeypatch):
    """embedding_serverへのHTTPリクエストをモック化する（vec_index再登録の検証用）。"""
    def mock_encode_batch(texts, prefix):
        embeddings = []
        for text in texts:
            prefix_str = "検索文書: " if prefix == "document" else "検索クエリ: "
            np.random.seed(hash(prefix_str + text) % (2**32))
            embeddings.append(np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist())
        return embeddings

    monkeypatch.setattr(emb, "_encode_batch", mock_encode_batch)
    monkeypatch.setattr(emb, "_server_initialized", True)
    monkeypatch.setattr(emb, "_backfill_done", True)
    yield


class TestRetractDecision:
    """decisionのretract"""

    def test_retract_decision(self, topic):
        """decisionをretractできる"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "テスト決定", "reason": "テスト理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        retract_result = retract("decision", [decision_id])
        assert "error" not in retract_result
        assert decision_id in retract_result["success"]
        assert retract_result["errors"] == []

        # DB上でもretracted_atが設定されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT retracted_at FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row["retracted_at"] is not None
        finally:
            conn.close()

    def test_retract_multiple_decisions(self, topic):
        """複数のdecisionを一括retractできる"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "決定1", "reason": "理由1"},
            {"topic_id": tid, "decision": "決定2", "reason": "理由2"},
        ])
        ids = [c["decision_id"] for c in result["created"]]

        retract_result = retract("decision", ids)
        assert len(retract_result["success"]) == 2
        assert retract_result["errors"] == []


class TestUnretractDecision:
    """decisionのun-retract"""

    def test_unretract_decision(self, topic):
        """retract済みdecisionをun-retractできる"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "テスト決定", "reason": "テスト理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        # retract → un-retract
        retract("decision", [decision_id])
        unretract_result = retract("decision", [decision_id], undo=True)

        assert "error" not in unretract_result
        assert decision_id in unretract_result["success"]

        # DB上でretracted_atがNULLに戻っていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT retracted_at FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row["retracted_at"] is None
        finally:
            conn.close()


class TestRetractLog:
    """logのretract"""

    def test_retract_log(self, topic):
        """logをretractできる"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "テストログ内容", "title": "テストログ"},
        ])
        log_id = result["created"][0]["log_id"]

        retract_result = retract("log", [log_id])
        assert "error" not in retract_result
        assert log_id in retract_result["success"]

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT retracted_at FROM discussion_logs WHERE id = ?", (log_id,)
            ).fetchone()
            assert row["retracted_at"] is not None
        finally:
            conn.close()

    def test_unretract_log(self, topic):
        """retract済みlogをun-retractできる"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "テストログ内容", "title": "テストログ"},
        ])
        log_id = result["created"][0]["log_id"]

        retract("log", [log_id])
        unretract_result = retract("log", [log_id], undo=True)

        assert "error" not in unretract_result
        assert log_id in unretract_result["success"]

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT retracted_at FROM discussion_logs WHERE id = ?", (log_id,)
            ).fetchone()
            assert row["retracted_at"] is None
        finally:
            conn.close()


class TestRetractIdempotent:
    """retract操作の冪等性"""

    def test_retract_twice_no_error(self, topic):
        """既にretracted状態でretractしても成功する"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "テスト決定", "reason": "テスト理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        result1 = retract("decision", [decision_id])
        result2 = retract("decision", [decision_id])

        assert "error" not in result1
        assert "error" not in result2
        assert decision_id in result2["success"]

    def test_unretract_nonretracted_no_error(self, topic):
        """retractされていない状態でun-retractしても成功する"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "テスト決定", "reason": "テスト理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        unretract_result = retract("decision", [decision_id], undo=True)
        assert "error" not in unretract_result
        assert decision_id in unretract_result["success"]


class TestRetractPartialSuccess:
    """部分成功"""

    def test_partial_success_with_nonexistent_id(self, topic):
        """存在するID + 存在しないIDで部分成功する"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "テスト決定", "reason": "テスト理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        retract_result = retract("decision", [decision_id, 99999])
        assert decision_id in retract_result["success"]
        assert len(retract_result["errors"]) == 1
        assert retract_result["errors"][0]["id"] == 99999
        assert "not found" in retract_result["errors"][0]["error"]["message"]


class TestRetractMaterial:
    """materialのretract/un-retract"""

    def test_retract_material(self, temp_db):
        """materialをretractするとmaterialsテーブルのretracted_atに現在時刻が設定される"""
        m = add_material(
            title="テスト資材",
            content="本文",
            tags=DEFAULT_TAGS,
            source="unit test",
        )
        material_id = m["material_id"]

        retract_result = retract("material", [material_id])
        assert "error" not in retract_result
        assert material_id in retract_result["success"]
        assert retract_result["errors"] == []

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT retracted_at FROM materials WHERE id = ?", (material_id,)
            ).fetchone()
            assert row["retracted_at"] is not None
        finally:
            conn.close()

    def test_unretract_material(self, temp_db):
        """retract済みmaterialをun-retractするとretracted_atがNULLに戻る"""
        m = add_material(
            title="テスト資材",
            content="本文",
            tags=DEFAULT_TAGS,
            source="unit test",
        )
        material_id = m["material_id"]

        retract("material", [material_id])
        unretract_result = retract("material", [material_id], undo=True)
        assert "error" not in unretract_result
        assert material_id in unretract_result["success"]

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT retracted_at FROM materials WHERE id = ?", (material_id,)
            ).fetchone()
            assert row["retracted_at"] is None
        finally:
            conn.close()

    def test_get_material_hides_retracted_by_default(self, temp_db):
        """get_materialはretract済みmaterialをデフォルトでNOT_FOUNDにする"""
        m = add_material(
            title="非表示資材",
            content="本文",
            tags=DEFAULT_TAGS,
            source="unit test",
        )
        material_id = m["material_id"]

        retract("material", [material_id])

        result = get_material(material_id)
        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"

    def test_get_material_include_retracted_returns_material(self, temp_db):
        """include_retracted=Trueを指定するとretract済みmaterialもretracted_at付きで取得できる"""
        m = add_material(
            title="復元用資材",
            content="本文",
            tags=DEFAULT_TAGS,
            source="unit test",
        )
        material_id = m["material_id"]

        retract("material", [material_id])

        result = get_material(material_id, include_retracted=True)
        assert "error" not in result
        assert result["material_id_raw"] == material_id
        assert "retracted_at" in result
        assert result["retracted_at"] is not None


class TestRetractValidationErrors:
    """バリデーションエラー"""

    def test_invalid_entity_type_topic(self, temp_db):
        """topicはretract対象外でバリデーションエラーになる"""
        result = retract("topic", [1])
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_entity_type_activity(self, temp_db):
        """activityはretract対象外でバリデーションエラーになる"""
        result = retract("activity", [1])
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "Invalid entity_type" in result["error"]["message"]

    def test_empty_ids(self, temp_db):
        """空のidsでバリデーションエラーになる"""
        result = retract("decision", [])
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "ids must not be empty" in result["error"]["message"]


class TestRetractWithPin:
    """pinsテーブルでpinされたエンティティのretractテスト"""

    def test_retract_pinned_decision(self, topic):
        """pinsテーブルでpinされたdecisionもretractできる（pinsエントリが残りretracted_atが設定される）"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "pinされた決定", "reason": "理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        # activityを作成してpinsテーブルにpin登録 → retract
        act = add_activity(title="テストタスク", description="テスト用", tags=["domain:test"], check_in=False)
        aid = act["activity_id"]
        add_pin("activity", aid, "decision", decision_id)
        retract_result = retract("decision", [decision_id])

        assert "error" not in retract_result
        assert decision_id in retract_result["success"]

        # pinsエントリが残りつつretracted_atが設定されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT retracted_at FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row["retracted_at"] is not None

            # pinsテーブルのエントリはそのまま残る（retract時pins残置）
            pin_row = conn.execute(
                "SELECT * FROM pins WHERE source_type='activity' AND source_id=? AND target_type='decision' AND target_id=?",
                (aid, decision_id),
            ).fetchone()
            assert pin_row is not None
        finally:
            conn.close()


class TestUnretractSearchIndexReregistration:
    """un-retract後のsearch_index/search_index_fts再登録

    retract済みmaterial/decisionは search_index/search_index_fts を物理削除される。
    un-retract時にこれらを明示的に再登録しないと、AFTER UPDATEトリガーが
    NULLなsearch_index.idでFTS5に自動採番させ、その採番idが後続の別エンティティの
    search_index.idと衝突しうる（衝突すると、取り消し済みエンティティの本文で検索
    したはずが無関係な別エンティティが返る）。ここではmaterial/decisionの双方で
    「un-retract後に自分自身が正しく検索でヒットする」「その後に追加した別エンティティ
    と search_index.id が衝突しない」ことをsearch()の実返り値で検証する。
    """

    def test_undo_material_hits_own_content_after_undo(self, temp_db):
        """un-retract後、materialは自分の本文で正しく検索にヒットする"""
        m = add_material(
            title="対象資材", content="本文にunique_marker_alpha を含む",
            tags=DEFAULT_TAGS, source="test",
        )
        material_id = m["material_id"]

        retract("material", [material_id])
        retract("material", [material_id], undo=True)

        result = search(keyword="unique_marker_alpha", entity_type="material")
        hit_ids = [r["id_raw"] for r in result["results"]]
        assert material_id in hit_ids

    def test_undo_then_add_material_no_id_collision(self, temp_db):
        """m2をretract→un-retractした後にm4を追加しても、m2の本文検索がm4を
        返すような search_index.id 衝突が起きない（バグ再現シナリオの回帰テスト）"""
        add_material(title="m1", content="m1 body", tags=DEFAULT_TAGS, source="test")
        m2 = add_material(
            title="m2", content="m2 body unique_marker_bravo",
            tags=DEFAULT_TAGS, source="test",
        )["material_id"]
        add_material(title="m3", content="m3 body", tags=DEFAULT_TAGS, source="test")

        retract("material", [m2])
        retract("material", [m2], undo=True)

        m4 = add_material(title="m4", content="m4 body unique_marker_charlie", tags=DEFAULT_TAGS, source="test")["material_id"]

        # m2の本文に含まれるマーカーで検索すると、最上位ヒットはm2自身（m4ではない）。
        # バグ再現時はここでm4がsearch_index.id衝突により最上位に来ていた。
        result_m2 = search(keyword="unique_marker_bravo", entity_type="material")
        assert result_m2["results"][0]["id_raw"] == m2

        # 逆方向: m4のマーカーで検索した最上位ヒットもm4自身
        result_m4 = search(keyword="unique_marker_charlie", entity_type="material")
        assert result_m4["results"][0]["id_raw"] == m4

    def test_undo_decision_hits_own_content_after_undo(self, topic):
        """un-retract後、decisionは自分の理由本文で正しく検索にヒットする
        (retract(undo=True)はentity_type非依存の共通実装のため、materialに限らず
        decision/logでも同じ経路でsearch_index再登録が必要)"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "対象決定", "reason": "unique_marker_delta を含む理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        retract("decision", [decision_id])
        retract("decision", [decision_id], undo=True)

        search_result = search(keyword="unique_marker_delta", entity_type="decision")
        hit_ids = [r["id_raw"] for r in search_result["results"]]
        assert decision_id in hit_ids

    def test_undo_then_add_decision_no_id_collision(self, topic):
        """decisionでもm2/m4パターンと同型のsearch_index.id衝突が起きない
        （retract undoはmaterial固有ではなく共通実装のため回帰する）"""
        tid = topic["topic_id"]
        add_decisions([{"topic_id": tid, "decision": "d1", "reason": "d1理由"}])
        d2 = add_decisions([
            {"topic_id": tid, "decision": "d2", "reason": "unique_marker_echo を含む理由"},
        ])["created"][0]["decision_id"]
        add_decisions([{"topic_id": tid, "decision": "d3", "reason": "d3理由"}])

        retract("decision", [d2])
        retract("decision", [d2], undo=True)

        d4 = add_decisions([
            {"topic_id": tid, "decision": "d4", "reason": "unique_marker_foxtrot を含む理由"},
        ])["created"][0]["decision_id"]

        # d2の理由に含まれるマーカーで検索すると、最上位ヒットはd2自身（d4ではない）。
        # バグ再現時はここでd4がsearch_index.id衝突により最上位に来ていた。
        result_d2 = search(keyword="unique_marker_echo", entity_type="decision")
        assert result_d2["results"][0]["id_raw"] == d2

        # 逆方向: d4のマーカーで検索した最上位ヒットもd4自身
        result_d4 = search(keyword="unique_marker_foxtrot", entity_type="decision")
        assert result_d4["results"][0]["id_raw"] == d4

    def test_undo_legacy_state_without_prior_physical_delete_is_idempotent(self, temp_db):
        """search_index行が既に存在する状態（retract_service導入前の物理削除を経ない
        古いretracted状態を模擬）でun-retractしても、search_index.idのUNIQUE制約
        違反（IntegrityError）を起こさず成功する"""
        m = add_material(title="m1", content="m1 body", tags=DEFAULT_TAGS, source="test")
        material_id = m["material_id"]

        conn = get_connection()
        try:
            conn.execute(
                "UPDATE materials SET retracted_at = '2020-01-01 00:00:00' WHERE id = ?",
                (material_id,),
            )
            conn.commit()
            si_row = conn.execute(
                "SELECT id FROM search_index WHERE source_type='material' AND source_id=?",
                (material_id,),
            ).fetchone()
            assert si_row is not None, "前提: search_index行が物理削除されず残っている状態"
        finally:
            conn.close()

        result = retract("material", [material_id], undo=True)
        assert "error" not in result
        assert material_id in result["success"]
        assert result["errors"] == []

    def test_undo_reregisters_vec_index(self, temp_db, mock_embedding_server):
        """un-retract後、vec_indexにも再登録される（embedding再生成、commit後ベストエフォート）"""
        m = add_material(title="m1", content="m1 body", tags=DEFAULT_TAGS, source="test")
        material_id = m["material_id"]

        retract("material", [material_id])

        conn = get_connection()
        try:
            si_row = conn.execute(
                "SELECT id FROM search_index WHERE source_type='material' AND source_id=?",
                (material_id,),
            ).fetchone()
            assert si_row is None, "retract直後はsearch_indexから物理削除されている"
        finally:
            conn.close()

        retract("material", [material_id], undo=True)

        conn = get_connection()
        try:
            si_row = conn.execute(
                "SELECT id FROM search_index WHERE source_type='material' AND source_id=?",
                (material_id,),
            ).fetchone()
            assert si_row is not None
            vec_row = conn.execute(
                "SELECT rowid FROM vec_index WHERE rowid = ?", (si_row["id"],)
            ).fetchone()
            assert vec_row is not None, "un-retract後にvec_indexへも再登録されるべき"
        finally:
            conn.close()
