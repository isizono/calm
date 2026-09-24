"""rules_promotion_pure の pure 関数群の単体テスト。

tag notes教訓のrules格上げ機構・初回設計案（資材「tag notes教訓のrules格上げ機構
初回設計案」）のEdge casesのうち、検出器の中核（1本目のPR）に掛かるものを対象とする:
セクション分解（退避索引の除外含む）、前文だけのタグ（intent名前空間の除外含む）、
共起タグの除外。
"""
from src.services.rules_promotion_pure import (
    INTENT_NAMESPACE,
    cluster_candidates,
    cosine_similarity,
    extract_detectable_units,
    find_similar_pairs,
    is_cooccurring_pair,
)

MULTI_SECTION_NOTES = (
    "## 教訓A\n"
    "本文A\n"
    "\n"
    "## 教訓B\n"
    "本文B\n"
    "\n"
    "## 退避済み（全文は資材へ）\n"
    "- 教訓C → get_material(material_id=1)\n"
    "\n"
    "#audited-2026-09-23\n"
)

PREAMBLE_ONLY_NOTES = "前文だけの教訓。見出しは無い。\n"

INTENT_PREAMBLE_NOTES = "作業フェーズの行動指示。前文のみ。\n"


class TestSectionDecomposition:
    """セクション分解: `## `見出し単位への分解、退避索引セクションの除外"""

    def test_splits_into_one_unit_per_section(self):
        units = extract_detectable_units("domain:calm", "domain", MULTI_SECTION_NOTES)

        assert len(units) == 2
        assert units[0]["tag"] == "domain:calm"
        assert units[0]["heading"] == "## 教訓A"
        assert "本文A" in units[0]["text"]
        assert units[1]["heading"] == "## 教訓B"
        assert "本文B" in units[1]["text"]

    def test_excludes_demote_index_section(self):
        units = extract_detectable_units("domain:calm", "domain", MULTI_SECTION_NOTES)

        headings = [u["heading"] for u in units]
        assert "## 退避済み（全文は資材へ）" not in headings

    def test_excludes_trailer_marker_line_from_any_unit(self):
        units = extract_detectable_units("domain:calm", "domain", MULTI_SECTION_NOTES)

        for unit in units:
            assert "#audited-2026-09-23" not in unit["text"]

    def test_archived_tag_returns_no_units(self):
        units = extract_detectable_units(
            "domain:calm", "domain", MULTI_SECTION_NOTES, archived=True
        )
        assert units == []


class TestPreambleOnlyTags:
    """前文だけのタグ: 前文全体を1単位とする。intent名前空間は検知対象から外す"""

    def test_non_intent_preamble_only_tag_becomes_single_unit(self):
        units = extract_detectable_units("hooks", "", PREAMBLE_ONLY_NOTES)

        assert len(units) == 1
        assert units[0]["heading"] is None
        assert units[0]["text"] == PREAMBLE_ONLY_NOTES

    def test_intent_namespace_preamble_only_tag_excluded(self):
        units = extract_detectable_units(
            "intent:design", INTENT_NAMESPACE, INTENT_PREAMBLE_NOTES
        )
        assert units == []

    def test_intent_namespace_with_sections_also_excluded(self):
        # intentタグは前文型に限らず名前空間そのもので除外される
        units = extract_detectable_units(
            "intent:design", INTENT_NAMESPACE, MULTI_SECTION_NOTES
        )
        assert units == []

    def test_empty_preamble_yields_no_units(self):
        units = extract_detectable_units("hooks", "", "")
        assert units == []


class TestCooccurringTagExclusion:
    """共起タグの除外: ほぼ常に一緒に付くタグ同士の類似ペアは候補クラスタに含めない"""

    def _make_units_and_embeddings(self):
        units = [
            {"tag": "domain:calm", "heading": "## 教訓X", "text": "共有ロジックの教訓X"},
            {"tag": "hooks", "heading": "## 教訓X'", "text": "共有ロジックの教訓Xの言い換え"},
        ]
        # 実質的に同一ベクトル（コサイン類似度1.0）にして類似判定を固定する
        embeddings = [[1.0, 0.0], [1.0, 0.0]]
        return units, embeddings

    def test_strongly_cooccurring_pair_is_excluded_from_clusters(self):
        units, embeddings = self._make_units_and_embeddings()
        pairs = find_similar_pairs(units, embeddings, threshold=0.9)
        assert len(pairs) == 1  # 前提: 類似ペアとしては検出されている

        # domain:calmとhooksが強く共起する（PMIがしきい値以上になる）カウントを与える。
        # 両タグとも出現が少数(20/1000)でほぼ重なる(18)ため、PMI = log2(0.018/(0.02*0.02)) ≈ 5.5
        co_counts = {("domain:calm", "hooks"): 18}
        usage_counts = {"domain:calm": 20, "hooks": 20}
        total = 1000

        clusters = cluster_candidates(
            units, pairs, co_counts, usage_counts, total, cooccurrence_threshold=2.0
        )
        assert clusters == []

    def test_independent_pair_forms_a_cluster(self):
        units, embeddings = self._make_units_and_embeddings()
        pairs = find_similar_pairs(units, embeddings, threshold=0.9)

        # domain:calmとhooksがほとんど共起しない（PMIが低い）カウントを与える
        co_counts = {("domain:calm", "hooks"): 1}
        usage_counts = {"domain:calm": 500, "hooks": 500}
        total = 1000

        clusters = cluster_candidates(
            units, pairs, co_counts, usage_counts, total, cooccurrence_threshold=2.0
        )
        assert len(clusters) == 1
        assert clusters[0]["tags"] == ["domain:calm", "hooks"]

    def test_is_cooccurring_pair_reads_reversed_key(self):
        # co_countsのキーが(tag_b, tag_a)の順で入っていても引ける
        co_counts = {("hooks", "domain:calm"): 18}
        usage_counts = {"domain:calm": 20, "hooks": 20}
        assert is_cooccurring_pair("domain:calm", "hooks", co_counts, usage_counts, 1000) is True

    def test_missing_pair_is_not_cooccurring(self):
        assert is_cooccurring_pair("a", "b", {}, {"a": 10, "b": 10}, 100) is False


class TestFindSimilarPairsSameTagExclusion:
    def test_same_tag_pair_is_never_returned(self):
        units = [
            {"tag": "domain:calm", "heading": "## A", "text": "教訓A"},
            {"tag": "domain:calm", "heading": "## B", "text": "教訓Aの言い換え"},
        ]
        embeddings = [[1.0, 0.0], [1.0, 0.0]]
        pairs = find_similar_pairs(units, embeddings, threshold=0.5)
        assert pairs == []

    def test_below_threshold_pair_is_excluded(self):
        units = [
            {"tag": "a", "heading": None, "text": "x"},
            {"tag": "b", "heading": None, "text": "y"},
        ]
        embeddings = [[1.0, 0.0], [0.0, 1.0]]  # 直交ベクトル: 類似度0.0
        pairs = find_similar_pairs(units, embeddings, threshold=0.5)
        assert pairs == []


class TestCosineSimilarity:
    def test_identical_vectors_have_similarity_one(self):
        assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0

    def test_orthogonal_vectors_have_similarity_zero(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_zero_vector_returns_zero(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
