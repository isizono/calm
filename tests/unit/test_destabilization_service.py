"""destabilization_service のテスト

resolve_destabilizationの3分岐（reaffirmed/revised/retracted）、
revised_to_decision_id必須バリデーション、冪等性、および
suggest_destabilized_candidatesの候補生成・スコアリング・縮退をカバーする。
"""
import os
import tempfile
import pytest
from sqlite_vec import serialize_float32

from src.db import init_database, get_connection
from src.services import precedent_pull_service as pps
from src.services.topic_service import add_topic
from src.services.decision_service import add_decisions
from src.services.destabilization_service import resolve_destabilization, suggest_destabilized_candidates
from src.services.relation_service import add_relation
from src.services.tag_service import _injected_tags
from tests.helpers import add_decision


DEFAULT_TAGS = ["domain:test"]
EMBEDDING_DIM = 384


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
def decisions(temp_db):
    """テスト用decisionを3件作成する（source=軸変更、target=影響先、revised_to=新結論）"""
    topic = add_topic(title="テストトピック", description="テスト用", tags=DEFAULT_TAGS)
    tid = topic["topic_id"]
    result = add_decisions([
        {"topic_id": tid, "decision": "軸変更decision", "reason": "軸変更理由"},
        {"topic_id": tid, "decision": "影響先decision", "reason": "影響先理由"},
        {"topic_id": tid, "decision": "改訂後decision", "reason": "改訂後理由"},
    ])
    created = result["created"]
    return {
        "source_id": created[0]["decision_id"],
        "target_id": created[1]["decision_id"],
        "revised_to_id": created[2]["decision_id"],
    }


@pytest.fixture
def mock_embedding_server(monkeypatch):
    """add_topic/add_decisions経由の書込を成立させるための最小モック。

    書き込まれる値そのものはテストで使わない（routingの距離制御は
    precedent_pull_service.encode_query の直接monkeypatchで行う）。
    """
    import src.services.embedding_service as emb

    def mock_encode_batch(texts, prefix):
        return [[0.001] * EMBEDDING_DIM for _ in texts]

    monkeypatch.setattr(emb, "_encode_batch", mock_encode_batch)
    monkeypatch.setattr(emb, "_server_initialized", True)
    monkeypatch.setattr(emb, "_backfill_done", True)


def _basis_vector(index: int, dim: int = EMBEDDING_DIM) -> list[float]:
    v = [0.0] * dim
    v[index] = 1.0
    return v


def _set_topic_vector(topic_id: int, vector: list[float]) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM topic_vec WHERE rowid = ?", (topic_id,))
        conn.execute(
            "INSERT INTO topic_vec(rowid, embedding) VALUES (?, ?)",
            (topic_id, serialize_float32(vector)),
        )
        conn.commit()
    finally:
        conn.close()


def _make_topic(title: str, vector_index: int, tags: list[str] | None = None) -> int:
    """topicを作成し、指定indexの基底ベクトルをtopic_vecに直接設定する。

    2つのtopicに同じvector_indexを与えれば距離0（routing閾値内=近傍）、
    異なるindexを与えれば直交（距離1.0、routing閾値超=routing miss）になる。
    """
    topic_id = add_topic(title=title, description="desc", tags=tags or DEFAULT_TAGS)["topic_id"]
    _set_topic_vector(topic_id, _basis_vector(vector_index))
    return topic_id


def _get_resolution_row(source_id: int, target_id: int):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT resolution, revised_to_decision_id, note FROM decision_destabilization_resolutions "
            "WHERE source_id = ? AND target_id = ?",
            (source_id, target_id),
        ).fetchone()
    finally:
        conn.close()


def _get_retracted_at(decision_id: int):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT retracted_at FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        return row["retracted_at"]
    finally:
        conn.close()


class TestResolveDestabilizationValidation:
    """引数バリデーション"""

    def test_invalid_resolution_value_rejected(self, decisions):
        """resolutionが3値以外だとVALIDATION_ERRORになり、行は追加されない"""
        d = decisions
        result = resolve_destabilization(d["source_id"], d["target_id"], "bogus")
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert _get_resolution_row(d["source_id"], d["target_id"]) is None

    def test_revised_without_revised_to_decision_id_rejected(self, decisions):
        """resolution='revised'でrevised_to_decision_id未指定だとVALIDATION_ERRORになり、行は追加されない"""
        d = decisions
        result = resolve_destabilization(d["source_id"], d["target_id"], "revised")
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert _get_resolution_row(d["source_id"], d["target_id"]) is None

    def test_reaffirmed_nonexistent_target_rejected(self, decisions):
        """reaffirmedで存在しないtarget_decision_idを指定するとFK制約違反でCONSTRAINT_VIOLATIONになり、行は追加されない"""
        d = decisions
        result = resolve_destabilization(d["source_id"], 999999, "reaffirmed")
        assert "error" in result
        assert result["error"]["code"] == "CONSTRAINT_VIOLATION"
        assert _get_resolution_row(d["source_id"], 999999) is None

    def test_retracted_nonexistent_target_rejected(self, decisions):
        """retractedで存在しないtarget_decision_idを指定すると、retract経路のnot-foundでITEM_ERRORになり、行は追加されない"""
        d = decisions
        result = resolve_destabilization(d["source_id"], 999999, "retracted")
        assert "error" in result
        assert result["error"]["code"] == "ITEM_ERROR"
        assert _get_resolution_row(d["source_id"], 999999) is None

    def test_retracted_nonexistent_source_rejected_without_retracting_target(self, decisions):
        """retractedで存在しないsource_decision_idを指定すると、targetをretractする前にCONSTRAINT_VIOLATIONで拒否される"""
        d = decisions
        result = resolve_destabilization(999999, d["target_id"], "retracted")
        assert "error" in result
        assert result["error"]["code"] == "CONSTRAINT_VIOLATION"
        assert _get_resolution_row(999999, d["target_id"]) is None
        # targetは実際にはretractされていない（副作用が発生する前に検証で止まる）
        assert _get_retracted_at(d["target_id"]) is None


class TestResolveDestabilizationReaffirmed:
    """resolution='reaffirmed'"""

    def test_reaffirmed_inserts_row_and_does_not_retract_target(self, decisions):
        """reaffirmedはresolution行のみINSERTし、targetのretracted_atは変化しない"""
        d = decisions
        result = resolve_destabilization(
            d["source_id"], d["target_id"], "reaffirmed", note="再確認した"
        )
        assert "error" not in result
        assert result == {"resolved": True, "already_resolved": False}

        row = _get_resolution_row(d["source_id"], d["target_id"])
        assert row is not None
        assert row["resolution"] == "reaffirmed"
        assert row["revised_to_decision_id"] is None
        assert row["note"] == "再確認した"

        assert _get_retracted_at(d["target_id"]) is None


class TestResolveDestabilizationRevised:
    """resolution='revised'"""

    def test_revised_inserts_row_with_revised_to_and_does_not_retract_target(self, decisions):
        """revisedはrevised_to_decision_idを記録し、targetのretracted_atは変化しない"""
        d = decisions
        result = resolve_destabilization(
            d["source_id"], d["target_id"], "revised",
            revised_to_decision_id=d["revised_to_id"],
        )
        assert "error" not in result
        assert result == {"resolved": True, "already_resolved": False}

        row = _get_resolution_row(d["source_id"], d["target_id"])
        assert row is not None
        assert row["resolution"] == "revised"
        assert row["revised_to_decision_id"] == d["revised_to_id"]

        assert _get_retracted_at(d["target_id"]) is None


class TestResolveDestabilizationRetracted:
    """resolution='retracted'"""

    def test_retracted_inserts_row_and_retracts_target(self, decisions):
        """retractedはresolution行をINSERTし、targetを実際にretractする"""
        d = decisions
        result = resolve_destabilization(d["source_id"], d["target_id"], "retracted")
        assert "error" not in result
        assert result == {"resolved": True, "already_resolved": False}

        row = _get_resolution_row(d["source_id"], d["target_id"])
        assert row is not None
        assert row["resolution"] == "retracted"

        assert _get_retracted_at(d["target_id"]) is not None


class TestResolveDestabilizationIdempotent:
    """同一エッジへの二重resolve"""

    def test_second_call_reports_already_resolved_without_duplicate_insert(self, decisions):
        """同一(source, target)への2回目の呼び出しはPK重複INSERTを起こさずalready_resolved=trueを返す"""
        d = decisions
        first = resolve_destabilization(d["source_id"], d["target_id"], "reaffirmed")
        assert first == {"resolved": True, "already_resolved": False}

        second = resolve_destabilization(d["source_id"], d["target_id"], "reaffirmed")
        assert "error" not in second
        assert second == {"resolved": False, "already_resolved": True}

        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM decision_destabilization_resolutions "
                "WHERE source_id = ? AND target_id = ?",
                (d["source_id"], d["target_id"]),
            ).fetchone()["cnt"]
            assert count == 1
        finally:
            conn.close()

    def test_second_call_with_retracted_does_not_re_trigger_retract_side_effect(self, decisions):
        """既にreaffirmed済みのエッジに対しretractedで再度呼んでも、targetは実際にはretractされない"""
        d = decisions
        resolve_destabilization(d["source_id"], d["target_id"], "reaffirmed")

        result = resolve_destabilization(d["source_id"], d["target_id"], "retracted")
        assert result == {"resolved": False, "already_resolved": True}

        # 既存行のresolutionは'reaffirmed'のまま（'retracted'に書き換わらない）
        row = _get_resolution_row(d["source_id"], d["target_id"])
        assert row["resolution"] == "reaffirmed"
        # targetは実際にはretractされていない
        assert _get_retracted_at(d["target_id"]) is None


class TestSuggestDestabilizedCandidatesTagOverlap:
    """タグ集合の重なりによるスコアリング（候補生成のtag共有チャネル）"""

    def test_higher_tag_overlap_scores_higher_and_ranks_above(
        self, temp_db, mock_embedding_server, monkeypatch
    ):
        """タグの重なりが多い候補ほどスコアが高く、上位にランクされる。
        両候補ともsourceのtopicとはrouting miss（直交ベクトル）の別topicに置き、
        embedding類似度の影響を排除してtag_jaccardの効果だけを見る。"""
        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        source_topic = _make_topic("軸変更topic", 0)
        source_id = add_decision(
            decision="軸変更", reason="r", topic_id=source_topic,
            tags=["domain:test", "alpha", "beta", "gamma"],
        )["decision_id"]

        # sourceのtopicとは直交（routing miss）だがタグを4つとも共有する候補
        other_topic = _make_topic("無関係topic", 2)
        high_overlap_id = add_decision(
            decision="高重複候補", reason="r", topic_id=other_topic,
            tags=["domain:test", "alpha", "beta", "gamma"],
        )["decision_id"]
        # 同じ無関係topicだが、タグ共有はdomain:test/alphaの2つだけの候補
        low_overlap_id = add_decision(
            decision="低重複候補", reason="r", topic_id=other_topic,
            tags=["alpha"],
        )["decision_id"]

        result = suggest_destabilized_candidates(source_id)

        assert result["mode"] == "vector"
        by_id = {c["decision_id"]: c for c in result["candidates"]}
        assert by_id[high_overlap_id]["score"] > by_id[low_overlap_id]["score"]
        ids_in_order = [c["decision_id"] for c in result["candidates"]]
        assert ids_in_order.index(high_overlap_id) < ids_in_order.index(low_overlap_id)
        # routing missのためembedding類似度は寄与していないことを確認
        assert not any(r.startswith("embedding_neighbor:") for r in by_id[high_overlap_id]["match_reason"])
        assert any(r.startswith("tag_overlap:") for r in by_id[high_overlap_id]["match_reason"])

    def test_candidates_sorted_by_score_descending(self, temp_db, mock_embedding_server, monkeypatch):
        """候補一覧全体がスコア降順で並んでいる"""
        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        topic_id = _make_topic("軸変更topic", 0)
        source_id = add_decision(
            decision="軸変更", reason="r", topic_id=topic_id, tags=["domain:test", "a", "b", "c"]
        )["decision_id"]
        for i, tags in enumerate([["a"], ["a", "b"], ["a", "b", "c"]]):
            add_decision(decision=f"候補{i}", reason="r", topic_id=topic_id, tags=tags)

        result = suggest_destabilized_candidates(source_id)

        scores = [c["score"] for c in result["candidates"]]
        assert scores == sorted(scores, reverse=True)
        assert len(scores) >= 3


class TestSuggestDestabilizedCandidatesEmbeddingNeighbor:
    """embedding近傍topicによる候補生成チャネル"""

    def test_topic_neighbor_without_tag_overlap_is_found_and_scored(
        self, temp_db, mock_embedding_server, monkeypatch
    ):
        """タグの重なりが無くても、sourceのtopicとembedding距離0（近傍）のtopicに
        属するdecisionは候補として発見され、embedding類似度分のスコアが付く"""
        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        source_topic = _make_topic("軸変更topic", 0, tags=["domain:test"])
        source_id = add_decision(
            decision="軸変更", reason="r", topic_id=source_topic, tags=["onlysource"]
        )["decision_id"]

        # sourceのtopicと同じ基底ベクトル（距離0=閾値内）だが、タグはsourceと無関係
        neighbor_topic = _make_topic("近傍topic", 0, tags=["domain:other"])
        neighbor_id = add_decision(
            decision="近傍候補", reason="r", topic_id=neighbor_topic, tags=["onlyneighbor"]
        )["decision_id"]

        result = suggest_destabilized_candidates(source_id)

        by_id = {c["decision_id"]: c for c in result["candidates"]}
        assert neighbor_id in by_id
        # distance=0（MISS_DISTANCE=0.19に対しsim=1.0）、タグ重なり無し（jaccard=0）、
        # 別topic（same_topic_bonus=0）なので、score = 0.3*1.0 + 0.6*0 + 0.1*0 = 0.3。
        # 重み定数の入れ替わりバグ（embedding/tag_jaccard weightの転置等）を検出できる
        # よう、単なる > 0 ではなく厳密値でassertする。
        assert by_id[neighbor_id]["score"] == pytest.approx(0.3)
        assert any(r.startswith("embedding_neighbor:") for r in by_id[neighbor_id]["match_reason"])
        assert not any(r.startswith("tag_overlap:") for r in by_id[neighbor_id]["match_reason"])

    def test_same_topic_bonus_reflected_in_match_reason(self, temp_db, mock_embedding_server, monkeypatch):
        """sourceと同じtopicに属する候補にはsame_topicのmatch_reasonが付く"""
        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        topic_id = _make_topic("軸変更topic", 0)
        source_id = add_decision(
            decision="軸変更", reason="r", topic_id=topic_id, tags=["domain:test"]
        )["decision_id"]
        same_topic_id = add_decision(
            decision="同topic候補", reason="r", topic_id=topic_id, tags=["domain:test"]
        )["decision_id"]

        result = suggest_destabilized_candidates(source_id)

        by_id = {c["decision_id"]: c for c in result["candidates"]}
        assert "same_topic" in by_id[same_topic_id]["match_reason"]


class TestSuggestDestabilizedCandidatesUnavailable:
    """embeddingサーバー停止時の縮退"""

    def test_embedding_unavailable_still_returns_tag_overlap_candidates(
        self, temp_db, mock_embedding_server, monkeypatch
    ):
        """route_topicsがmode=unavailableを返す場合でも、embeddingに依存しない
        タグ一致チャネル(a)の候補は例外にせず引き続き返され、mode="tag_only"になる
        （embedding近傍チャネル(b)のみが無効化される）"""
        monkeypatch.setattr(pps, "encode_query", lambda context: None)

        topic_id = _make_topic("軸変更topic", 0)
        source_id = add_decision(
            decision="軸変更", reason="r", topic_id=topic_id, tags=["domain:test"]
        )["decision_id"]
        tag_match_id = add_decision(
            decision="候補", reason="r", topic_id=topic_id, tags=["domain:test"]
        )["decision_id"]

        result = suggest_destabilized_candidates(source_id)

        assert result["mode"] == "tag_only"
        by_id = {c["decision_id"]: c for c in result["candidates"]}
        assert tag_match_id in by_id
        assert any(r.startswith("tag_overlap:") for r in by_id[tag_match_id]["match_reason"])
        # embeddingチャネルは無効化されているのでembedding_neighbor理由は付かない
        assert not any(r.startswith("embedding_neighbor:") for r in by_id[tag_match_id]["match_reason"])

    def test_embedding_unavailable_with_no_tag_overlap_returns_empty_tag_only(
        self, temp_db, mock_embedding_server, monkeypatch
    ):
        """embeddingチャネルが無効化され、タグ一致チャネルにも候補が無い場合は
        空candidatesだがmode="tag_only"のまま（"unavailable"には戻らない）"""
        monkeypatch.setattr(pps, "encode_query", lambda context: None)

        topic_id = _make_topic("孤立topic", 0, tags=["domain:calm"])
        source_id = add_decision(decision="軸変更", reason="r", topic_id=topic_id, tags=None)["decision_id"]

        result = suggest_destabilized_candidates(source_id)

        assert result == {"candidates": [], "mode": "tag_only"}


class TestSuggestDestabilizedCandidatesEmptyCandidates:
    """候補が見つからない場合の縮退（空タグ相当のケース）"""

    @pytest.mark.parametrize("excluded_domain_tag", ["domain:calm", "domain:cc-memory"])
    def test_no_matching_tags_and_no_other_decisions_returns_empty_without_exception(
        self, temp_db, mock_embedding_server, monkeypatch, excluded_domain_tag
    ):
        """sourceの有効タグが候補生成の除外タグしか無く、DB内に他のdecision・
        近傍topicも存在しない場合、例外にならず空candidatesを返す。

        domain:cc-memory → domain:calm のタグrenameはこのコードのマージ後に実施するため、
        DB上の実タグ名が新旧どちらであっても除外が効く必要がある（片方でも除外が外れると
        候補集合が実質DB全体に膨らむ）。両方の名前でパラメタライズして担保する。
        """
        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        topic_id = _make_topic("孤立topic", 0, tags=[excluded_domain_tag])
        source_id = add_decision(decision="軸変更", reason="r", topic_id=topic_id, tags=None)["decision_id"]

        result = suggest_destabilized_candidates(source_id)

        assert result == {"candidates": [], "mode": "vector"}


class TestSuggestDestabilizedCandidatesFlags:
    """already_destabilized / already_resolved フラグと重複排除"""

    def test_resolved_target_excluded_by_default_unresolved_target_flagged(
        self, temp_db, mock_embedding_server, monkeypatch
    ):
        """destabilizesエッジが張られた候補のうちresolve済みのものは既定で除外され、
        未resolveのものはalready_destabilized=Trueで残る"""
        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        topic_id = _make_topic("軸変更topic", 0)
        source_id = add_decision(
            decision="軸変更", reason="r", topic_id=topic_id, tags=["domain:test", "shared"]
        )["decision_id"]
        target_a = add_decision(
            decision="影響先A", reason="r", topic_id=topic_id, tags=["domain:test", "shared"]
        )["decision_id"]
        target_b = add_decision(
            decision="影響先B", reason="r", topic_id=topic_id, tags=["domain:test", "shared"]
        )["decision_id"]

        add_relation(
            "decision", source_id, [{"type": "decision", "ids": [target_a, target_b]}],
            relation_type="destabilizes",
        )
        resolve_destabilization(source_id, target_a, "reaffirmed")

        result = suggest_destabilized_candidates(source_id)
        by_id = {c["decision_id"]: c for c in result["candidates"]}

        assert target_a not in by_id
        assert by_id[target_b]["already_destabilized"] is True
        assert by_id[target_b]["already_resolved"] is False

    def test_include_already_resolved_true_includes_resolved_candidate_with_flags(
        self, temp_db, mock_embedding_server, monkeypatch
    ):
        """include_already_resolved=Trueのときはresolve済み候補も含まれ、
        already_destabilized/already_resolvedとも正しくTrueになる"""
        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        topic_id = _make_topic("軸変更topic", 0)
        source_id = add_decision(
            decision="軸変更", reason="r", topic_id=topic_id, tags=["domain:test", "shared"]
        )["decision_id"]
        target_a = add_decision(
            decision="影響先A", reason="r", topic_id=topic_id, tags=["domain:test", "shared"]
        )["decision_id"]

        add_relation(
            "decision", source_id, [{"type": "decision", "ids": [target_a]}],
            relation_type="destabilizes",
        )
        resolve_destabilization(source_id, target_a, "reaffirmed")

        result = suggest_destabilized_candidates(source_id, include_already_resolved=True)
        by_id = {c["decision_id"]: c for c in result["candidates"]}

        assert by_id[target_a]["already_destabilized"] is True
        assert by_id[target_a]["already_resolved"] is True

    def test_k_limits_returned_candidate_count(self, temp_db, mock_embedding_server, monkeypatch):
        """kで返す候補数の上限を制御できる"""
        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        topic_id = _make_topic("軸変更topic", 0)
        source_id = add_decision(
            decision="軸変更", reason="r", topic_id=topic_id, tags=["domain:test", "shared"]
        )["decision_id"]
        for i in range(5):
            add_decision(
                decision=f"候補{i}", reason="r", topic_id=topic_id, tags=["domain:test", "shared"]
            )

        result = suggest_destabilized_candidates(source_id, k=2)

        assert len(result["candidates"]) == 2


class TestSuggestDestabilizedCandidatesReadOnly:
    """read-only性（decision_supersedes等に一切書き込みが発生しないこと）"""

    def test_call_does_not_write_to_decision_supersedes_or_resolutions(
        self, temp_db, mock_embedding_server, monkeypatch
    ):
        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        topic_id = _make_topic("軸変更topic", 0)
        source_id = add_decision(
            decision="軸変更", reason="r", topic_id=topic_id, tags=["domain:test", "shared"]
        )["decision_id"]
        add_decision(decision="候補", reason="r", topic_id=topic_id, tags=["domain:test", "shared"])

        suggest_destabilized_candidates(source_id)

        conn = get_connection()
        try:
            supersedes_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM decision_supersedes"
            ).fetchone()["cnt"]
            resolutions_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM decision_destabilization_resolutions"
            ).fetchone()["cnt"]
        finally:
            conn.close()

        assert supersedes_count == 0
        assert resolutions_count == 0
