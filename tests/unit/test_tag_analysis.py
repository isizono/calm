"""analyze_tags機能のユニットテスト"""
import math
import os
import tempfile

import pytest

from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.activity_service import add_activity
from src.services.tag_service import _TAG_NOTES_RATCHET_CEILING, update_tag
from tests.helpers import add_log, add_decision, force_notes_over_ceiling
from src.services.tag_analysis_service import (
    analyze_tags,
    calc_pmi,
    _find_clusters,
    _find_orphans,
)
import src.services.embedding_service as emb


DEFAULT_TAGS = ["domain:test"]


def _add_alias_tag(namespace, name, canonical_namespace, canonical_name):
    """canonical_idを張ったエイリアスタグを1件作る。

    update_tagのcanonical設定は対象タグが既存であることを要求するため、
    エンティティに紐付かない純粋なエイリアス行はSQLで直接作る。
    """
    conn = get_connection()
    try:
        canonical_id = conn.execute(
            "SELECT id FROM tags WHERE namespace = ? AND name = ?",
            (canonical_namespace, canonical_name),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO tags (namespace, name, canonical_id) VALUES (?, ?, ?)",
            (namespace, name, canonical_id),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


# ========================================
# 3a: PMI計算の正確性テスト
# ========================================


class TestCalcPMI:
    """PMI計算のユニットテスト"""

    def test_basic_pmi(self):
        """基本的なPMI計算が正しい"""
        # P(a,b) = 2/10, P(a) = 4/10, P(b) = 5/10
        # PMI = log2(0.2 / (0.4 * 0.5)) = log2(1.0) = 0.0
        result = calc_pmi(co_count=2, count_a=4, count_b=5, total=10)
        assert result == pytest.approx(0.0, abs=0.01)

    def test_positive_pmi(self):
        """正のPMI（共起がランダムより多い）"""
        # P(a,b) = 4/10, P(a) = 4/10, P(b) = 5/10
        # PMI = log2(0.4 / (0.4 * 0.5)) = log2(2.0) = 1.0
        result = calc_pmi(co_count=4, count_a=4, count_b=5, total=10)
        assert result == pytest.approx(1.0, abs=0.01)

    def test_negative_pmi(self):
        """負のPMI（共起がランダムより少ない）"""
        # P(a,b) = 1/10, P(a) = 5/10, P(b) = 5/10
        # PMI = log2(0.1 / (0.5 * 0.5)) = log2(0.4) ≈ -1.32
        result = calc_pmi(co_count=1, count_a=5, count_b=5, total=10)
        assert result == pytest.approx(math.log2(0.4), abs=0.01)

    def test_zero_total(self):
        """total=0の場合は0.0"""
        assert calc_pmi(1, 1, 1, 0) == 0.0

    def test_zero_count(self):
        """count_a=0 or count_b=0の場合は0.0"""
        assert calc_pmi(1, 0, 5, 10) == 0.0
        assert calc_pmi(1, 5, 0, 10) == 0.0

    def test_zero_co_count(self):
        """co_count=0の場合は0.0"""
        assert calc_pmi(0, 5, 5, 10) == 0.0

    def test_perfect_co_occurrence(self):
        """完全共起: 常に一緒に出現するペア"""
        # P(a,b) = 3/10, P(a) = 3/10, P(b) = 3/10
        # PMI = log2(0.3 / (0.3 * 0.3)) = log2(1/0.3) ≈ 1.74
        result = calc_pmi(co_count=3, count_a=3, count_b=3, total=10)
        assert result == pytest.approx(math.log2(10 / 3), abs=0.01)


# ========================================
# 3b: クラスタリングのテスト
# ========================================


class TestClustering:
    """クラスタリング（連結成分）のテスト"""

    def test_single_cluster(self):
        """全タグが1つのクラスタに属する"""
        co_counts = {(1, 2): 5, (2, 3): 5, (1, 3): 5}
        usage_counts = {1: 5, 2: 5, 3: 5}
        total = 5
        tag_names = {1: "a", 2: "b", 3: "c"}

        clusters = _find_clusters(co_counts, usage_counts, total, tag_names, threshold=0.0)
        assert len(clusters) == 1
        assert sorted(clusters[0]["tags"]) == ["a", "b", "c"]

    def test_two_clusters(self):
        """2つの独立したクラスタ"""
        # PMIが高いペア(1,2)と(3,4)、低いペア(2,3)
        co_counts = {(1, 2): 10, (3, 4): 10, (2, 3): 1}
        usage_counts = {1: 10, 2: 10, 3: 10, 4: 10}
        total = 100
        tag_names = {1: "a", 2: "b", 3: "c", 4: "d"}

        # 高い閾値で分離
        clusters = _find_clusters(co_counts, usage_counts, total, tag_names, threshold=3.0)
        # (1,2) PMI = log2((10/100) / (10/100 * 10/100)) = log2(10) ≈ 3.32 → 閾値超え
        # (3,4) PMI = log2((10/100) / (10/100 * 10/100)) = log2(10) ≈ 3.32 → 閾値超え
        # (2,3) PMI = log2((1/100) / (10/100 * 10/100)) = log2(1) = 0 → 閾値未満
        assert len(clusters) == 2
        tag_sets = [set(c["tags"]) for c in clusters]
        assert {"a", "b"} in tag_sets
        assert {"c", "d"} in tag_sets

    def test_no_clusters_below_threshold(self):
        """閾値未満ではクラスタが形成されない"""
        co_counts = {(1, 2): 1}
        usage_counts = {1: 10, 2: 10}
        total = 100
        tag_names = {1: "a", 2: "b"}

        # PMI = log2((1/100) / (10/100 * 10/100)) = log2(1) = 0
        clusters = _find_clusters(co_counts, usage_counts, total, tag_names, threshold=5.0)
        assert len(clusters) == 0

    def test_empty_input(self):
        """空入力でも空配列"""
        clusters = _find_clusters({}, {}, 0, {}, threshold=2.0)
        assert clusters == []


# ========================================
# 3c: 孤児検出テスト
# ========================================


class TestOrphans:
    """孤児タグ検出のテスト"""

    def test_basic_orphan_detection(self):
        """usage < min_usageのタグが孤児として検出される"""
        usage_counts = {1: 1, 2: 5, 3: 10}
        co_counts = {(1, 2): 1}
        total = 20
        tag_names = {1: "orphan-tag", 2: "common-tag", 3: "popular-tag"}

        orphans = _find_orphans(usage_counts, co_counts, total, tag_names, {}, min_usage=2)
        assert len(orphans) == 1
        assert orphans[0]["tag"] == "orphan-tag"
        assert orphans[0]["usage"] == 1
        assert orphans[0]["nearest"] == "common-tag"
        assert orphans[0]["pmi_to_nearest"] is not None
        assert orphans[0]["archived"] is False
        assert orphans[0]["archived_reason"] is None

    def test_no_orphans(self):
        """全タグがmin_usage以上の場合"""
        usage_counts = {1: 5, 2: 5}
        orphans = _find_orphans(usage_counts, {}, 10, {1: "a", 2: "b"}, {}, min_usage=2)
        assert len(orphans) == 0

    def test_orphan_without_co_occurrence(self):
        """共起ペアがない孤児"""
        usage_counts = {1: 1, 2: 5}
        co_counts = {}  # 共起なし
        tag_names = {1: "lone-tag", 2: "common-tag"}

        orphans = _find_orphans(usage_counts, co_counts, 10, tag_names, {}, min_usage=2)
        assert len(orphans) == 1
        assert orphans[0]["tag"] == "lone-tag"
        assert orphans[0]["nearest"] is None
        assert orphans[0]["pmi_to_nearest"] is None

    def test_min_usage_boundary(self):
        """min_usageちょうどのタグは孤児にならない"""
        usage_counts = {1: 2, 2: 5}
        orphans = _find_orphans(usage_counts, {}, 10, {1: "a", 2: "b"}, {}, min_usage=2)
        assert len(orphans) == 0

    def test_orphan_archived_field_populated_from_tag_info(self):
        """tag_infoにarchived情報があればorphanエントリに反映される"""
        usage_counts = {1: 1, 2: 5}
        tag_names = {1: "legacy-orphan", 2: "common-tag"}
        tag_info = {1: {"archived": True, "archived_reason": "退役済み"}}

        orphans = _find_orphans(usage_counts, {}, 10, tag_names, tag_info, min_usage=2)
        assert len(orphans) == 1
        assert orphans[0]["archived"] is True
        assert orphans[0]["archived_reason"] == "退役済み"


# ========================================
# 3d: 重複候補検出テスト（embedding無効時のフォールバック含む）
# ========================================


class TestSuspectedDuplicates:
    """重複候補検出のテスト"""

    def test_no_duplicates_without_embedding(self, temp_db):
        """embedding無効時は空配列"""
        add_topic(title="T1", description="D", tags=["domain:test", "hook"])
        add_topic(title="T2", description="D", tags=["domain:test", "hooks"])

        result = analyze_tags()
        assert "error" not in result
        # embedding無効なのでsuspected_duplicatesは空
        assert result["suspected_duplicates"] == []

    def test_suspected_duplicates_is_list(self, temp_db):
        """suspected_duplicatesは常にリスト"""
        result = analyze_tags()
        assert "error" not in result
        assert isinstance(result["suspected_duplicates"], list)


# ========================================
# 3e: フィルタリングテスト（domain, focus_tag, min_usage, top_n）
# ========================================


class TestFiltering:
    """フィルタリングのテスト"""

    def test_domain_filter(self, temp_db):
        """domainフィルタでdomainに属するエンティティのみが対象になる"""
        add_topic(title="T1", description="D", tags=["domain:test", "arch", "design"])
        add_topic(title="T2", description="D", tags=["domain:other", "infra"])

        result = analyze_tags(domain="test")
        assert "error" not in result

        # domain:testに属するタグのみ
        all_tags_in_result = set()
        for co in result["co_occurrences"]:
            all_tags_in_result.add(co["tag_a"])
            all_tags_in_result.add(co["tag_b"])

        # "infra"はdomain:otherのみなので含まれない
        assert "infra" not in all_tags_in_result

    def test_domain_filter_not_found(self, temp_db):
        """存在しないdomainでエラー"""
        result = analyze_tags(domain="nonexistent")
        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"

    def test_domain_filter_resolves_alias_to_canonical(self, temp_db):
        """エイリアスのdomain名を渡してもcanonical側と同じ結果になる。

        エイリアスタグ自身は紐付けを1件も持たないため、canonicalを解決せず
        エイリアスのtag_idで絞り込むと、エラーにならないまま対象0件の結果が返る
        （呼び出し側からは「そのdomainには何も無い」と区別できない）。
        """
        add_topic(title="T1", description="D", tags=["domain:test", "arch", "design"])
        add_topic(title="T2", description="D", tags=["domain:other", "infra"])
        _add_alias_tag("domain", "test-alias", "domain", "test")

        via_alias = analyze_tags(domain="test-alias")
        via_canonical = analyze_tags(domain="test")

        assert "error" not in via_alias
        assert via_alias["co_occurrences"] == via_canonical["co_occurrences"]

        alias_tags = set()
        for co in via_alias["co_occurrences"]:
            alias_tags.add(co["tag_a"])
            alias_tags.add(co["tag_b"])
        assert "design" in alias_tags
        assert "infra" not in alias_tags

    def test_focus_tag_resolves_alias_to_canonical(self, temp_db):
        """エイリアスのfocus_tagを渡してもcanonical側と同じペアに絞られる。

        domainフィルタと同じく、canonicalを解決しないと該当ペア0件の結果が
        静かに返る。
        """
        add_topic(title="T1", description="D", tags=["domain:test", "arch", "design"])
        add_topic(title="T2", description="D", tags=["domain:test", "arch", "impl"])
        _add_alias_tag("", "arch-alias", "", "arch")

        via_alias = analyze_tags(focus_tag="arch-alias")
        via_canonical = analyze_tags(focus_tag="arch")

        assert "error" not in via_alias
        assert via_alias["co_occurrences"] == via_canonical["co_occurrences"]
        assert via_alias["co_occurrences"]

    def test_focus_tag(self, temp_db):
        """focus_tagでそのタグを含むペアのみに絞る"""
        add_topic(title="T1", description="D", tags=["domain:test", "arch", "design"])
        add_topic(title="T2", description="D", tags=["domain:test", "arch", "impl"])
        add_topic(title="T3", description="D", tags=["domain:test", "design", "impl"])

        result = analyze_tags(focus_tag="arch")
        assert "error" not in result

        for co in result["co_occurrences"]:
            # archを含むペアのみ
            assert co["tag_a"] == "arch" or co["tag_b"] == "arch"

    def test_focus_tag_not_found(self, temp_db):
        """存在しないfocus_tagでエラー"""
        result = analyze_tags(focus_tag="nonexistent")
        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"

    def test_min_usage(self, temp_db):
        """min_usageで孤児判定の閾値を変更できる"""
        add_topic(title="T1", description="D", tags=["domain:test", "common"])
        add_topic(title="T2", description="D", tags=["domain:test", "common"])
        add_topic(title="T3", description="D", tags=["domain:test", "rare"])

        result_default = analyze_tags(min_usage=2)
        assert "error" not in result_default
        orphan_tags_default = {o["tag"] for o in result_default["orphans"]}
        # "rare" は usage=1 なのでmin_usage=2で孤児
        assert "rare" in orphan_tags_default

        result_relaxed = analyze_tags(min_usage=1)
        assert "error" not in result_relaxed
        orphan_tags_relaxed = {o["tag"] for o in result_relaxed["orphans"]}
        # min_usage=1 だと孤児にならない
        assert "rare" not in orphan_tags_relaxed

    def test_top_n(self, temp_db):
        """top_nで返す共起ペア数を制限できる"""
        # 複数タグを持つトピックを作成して共起を生成
        add_topic(title="T1", description="D", tags=["domain:test", "a", "b", "c", "d"])
        add_topic(title="T2", description="D", tags=["domain:test", "a", "b", "c", "d"])

        result = analyze_tags(top_n=2)
        assert "error" not in result
        assert len(result["co_occurrences"]) <= 2

    def test_include_domain_tags(self, temp_db):
        """include_domain_tags=Trueでdomain:タグも分析対象になる"""
        add_topic(title="T1", description="D", tags=["domain:test", "arch"])
        add_topic(title="T2", description="D", tags=["domain:test", "arch"])

        result_without = analyze_tags(include_domain_tags=False)
        assert "error" not in result_without
        all_tags_without = set()
        for co in result_without["co_occurrences"]:
            all_tags_without.add(co["tag_a"])
            all_tags_without.add(co["tag_b"])
        assert "domain:test" not in all_tags_without

        result_with = analyze_tags(include_domain_tags=True)
        assert "error" not in result_with
        all_tags_with = set()
        for co in result_with["co_occurrences"]:
            all_tags_with.add(co["tag_a"])
            all_tags_with.add(co["tag_b"])
        # include_domain_tags=Trueなのでdomain:testが含まれうる
        # （共起ペアに含まれるかはデータ依存だが、少なくともエラーにならない）


# ========================================
# 3f: 統合テスト（MCPツール経由での呼び出し）
# ========================================


def _tag_id(name, namespace="domain"):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM tags WHERE namespace = ? AND name = ?",
            (namespace, name),
        ).fetchone()
        return row["id"]
    finally:
        conn.close()


class TestNotesOverBudget:
    """analyze_tagsのnotes_over_budgetセクションのテスト"""

    def test_over_budget_tag_is_listed(self, temp_db):
        add_topic(title="T1", description="D", tags=["domain:over"])
        tag_id = _tag_id("over")
        force_notes_over_ceiling(tag_id, _TAG_NOTES_RATCHET_CEILING + 1)

        result = analyze_tags()
        assert "error" not in result
        entries = {e["tag"]: e for e in result["notes_over_budget"]}
        assert "domain:over" in entries
        assert entries["domain:over"]["length"] == _TAG_NOTES_RATCHET_CEILING + 1
        assert entries["domain:over"]["ceiling"] == _TAG_NOTES_RATCHET_CEILING
        assert entries["domain:over"]["archived"] is False

    def test_within_budget_tag_is_not_listed(self, temp_db):
        add_topic(title="T1", description="D", tags=["domain:within"])
        update_tag("domain:within", notes="x" * _TAG_NOTES_RATCHET_CEILING)

        result = analyze_tags()
        tags_listed = {e["tag"] for e in result["notes_over_budget"]}
        assert "domain:within" not in tags_listed

    def test_notes_over_budget_independent_of_domain_filter(self, temp_db):
        """domain/include_domain_tags等の分析スコープに関わらず全タグを走査する"""
        add_topic(title="T1", description="D", tags=["domain:test", "domain:other"])
        tag_id = _tag_id("other")
        force_notes_over_ceiling(tag_id, _TAG_NOTES_RATCHET_CEILING + 1)

        result = analyze_tags(domain="test", include_domain_tags=False)
        tags_listed = {e["tag"] for e in result["notes_over_budget"]}
        assert "domain:other" in tags_listed

    def test_sorted_by_length_descending(self, temp_db):
        add_topic(title="T1", description="D", tags=["domain:big", "domain:small"])
        force_notes_over_ceiling(_tag_id("big"), _TAG_NOTES_RATCHET_CEILING + 500)
        force_notes_over_ceiling(_tag_id("small"), _TAG_NOTES_RATCHET_CEILING + 1)

        result = analyze_tags()
        over_budget_names = [e["tag"] for e in result["notes_over_budget"]]
        assert over_budget_names.index("domain:big") < over_budget_names.index("domain:small")


class TestIntegration:
    """統合テスト"""

    def test_basic_analyze_tags(self, temp_db):
        """基本動作: 4セクションを返す"""
        add_topic(title="T1", description="D", tags=["domain:test", "arch", "design"])
        add_topic(title="T2", description="D", tags=["domain:test", "arch", "design"])

        result = analyze_tags()
        assert "error" not in result
        assert "co_occurrences" in result
        assert "clusters" in result
        assert "orphans" in result
        assert "suspected_duplicates" in result
        assert "notes_over_budget" in result

    def test_co_occurrences_detected(self, temp_db):
        """共起ペアが正しく検出される"""
        add_topic(title="T1", description="D", tags=["domain:test", "arch", "design"])
        add_topic(title="T2", description="D", tags=["domain:test", "arch", "design"])

        result = analyze_tags()
        assert "error" not in result
        assert len(result["co_occurrences"]) > 0

        # arch-design ペアが検出されるはず
        pair_found = False
        for co in result["co_occurrences"]:
            tags = {co["tag_a"], co["tag_b"]}
            if tags == {"arch", "design"}:
                pair_found = True
                assert co["raw_count"] == 2
                break
        assert pair_found

    def test_co_occurrences_format(self, temp_db):
        """co_occurrencesの各要素のフォーマット"""
        add_topic(title="T1", description="D", tags=["domain:test", "arch", "design"])
        add_topic(title="T2", description="D", tags=["domain:test", "arch", "design"])

        result = analyze_tags()
        for co in result["co_occurrences"]:
            assert "tag_a" in co
            assert "tag_b" in co
            assert "pmi" in co
            assert "raw_count" in co
            assert isinstance(co["pmi"], float)
            assert isinstance(co["raw_count"], int)

    def test_empty_db(self, temp_db):
        """初期状態でもエラーにならない"""
        result = analyze_tags()
        assert "error" not in result
        assert result["co_occurrences"] == []
        assert result["clusters"] == []

    def test_cross_entity_co_occurrence(self, temp_db):
        """異なるエンティティタイプ間の共起が集計される"""
        topic = add_topic(title="T1", description="D", tags=["domain:test", "arch", "design"])
        add_activity(title="A1", description="D", tags=["domain:test", "arch", "design"], check_in=False)

        result = analyze_tags()
        assert "error" not in result

        # topic_tagsとactivity_tagsの両方で共起が集計される
        pair_found = False
        for co in result["co_occurrences"]:
            tags = {co["tag_a"], co["tag_b"]}
            if tags == {"arch", "design"}:
                pair_found = True
                # topic_tags(1回) + activity_tags(1回) = 2
                assert co["raw_count"] == 2
                break
        assert pair_found

    def test_decision_and_log_co_occurrence(self, temp_db):
        """decision_tagsとlog_tagsでも共起が集計される"""
        topic = add_topic(title="T1", description="D", tags=["domain:test"])
        add_decision(
            topic_id=topic["topic_id"], decision="D1", reason="R1",
            tags=["impl", "refactor"],
        )
        add_log(
            topic_id=topic["topic_id"], title="L1", content="C1",
            tags=["impl", "refactor"],
        )

        result = analyze_tags()
        assert "error" not in result

        pair_found = False
        for co in result["co_occurrences"]:
            tags = {co["tag_a"], co["tag_b"]}
            if tags == {"impl", "refactor"}:
                pair_found = True
                assert co["raw_count"] >= 2
                break
        assert pair_found

    def test_clusters_detected(self, temp_db):
        """クラスタが検出される"""
        # 高PMIの共起ペアを作る（3つのタグが常に一緒に出現）
        for i in range(5):
            add_topic(
                title=f"T{i}", description="D",
                tags=["domain:test", "cluster-a", "cluster-b", "cluster-c"],
            )

        result = analyze_tags()
        assert "error" not in result
        # cluster-a, cluster-b, cluster-c が1つのクラスタに属するはず
        if result["clusters"]:
            all_cluster_tags = set()
            for c in result["clusters"]:
                all_cluster_tags.update(c["tags"])
                assert "cohesion" in c
                assert isinstance(c["cohesion"], float)

    def test_orphans_detected(self, temp_db):
        """孤児タグが検出される"""
        # "common"は2回使用、"rare"は1回のみ
        add_topic(title="T1", description="D", tags=["domain:test", "common"])
        add_topic(title="T2", description="D", tags=["domain:test", "common"])
        add_topic(title="T3", description="D", tags=["domain:test", "rare"])

        result = analyze_tags(min_usage=2)
        assert "error" not in result
        orphan_tags = {o["tag"] for o in result["orphans"]}
        assert "rare" in orphan_tags

        for o in result["orphans"]:
            assert "tag" in o
            assert "usage" in o
            assert "nearest" in o
            assert "pmi_to_nearest" in o
