"""import_bundle_service(dry_run)の統合テスト

export_bundleで実際に書き出したバンドルを、別のDB(別インスタンスを模す)へ
import_bundle(mode="dry_run")で読み込み、以下を検証する:
- 副作用ゼロ(DBのエンティティ数・import_provenance件数が変化しない)
- 再import判定4状態(new/unchanged/updatable/upstream_changed_skip/self_origin)
- provenance UNIQUE制約と同じキーでの冪等判定
- タグ4区分レポート(merge/create/archived_hit/alias_hit)とnotes展開
- dangling refsの検知(バンドル内/provenance/自インスタンス出生の各解決経路との対比)
- ネイティブ重複疑い検知とembedding未起動時のdegraded表示
"""
import os
import tempfile

import numpy as np
import pytest

from src.db import get_connection, init_database
from src.services.activity_service import add_activity, update_activity
from src.services.export_bundle_service import export_bundle
from src.services.import_bundle_service import import_bundle
from src.services.instance_service import set_instance_identity
from src.services.material_service import add_material
from src.services.relation_service import add_relation
from src.services.retract_service import retract
from src.services.search_service import search
from src.services.tag_service import _injected_tags, update_tag
from src.services.topic_service import add_topic
from tests.helpers import add_decision, add_log

DEFAULT_TAGS = ["domain:test-import"]
EMBEDDING_DIM = 384


def _switch_db(db_path: str) -> None:
    os.environ["DISCUSSION_DB_PATH"] = db_path
    init_database()
    _injected_tags.clear()


@pytest.fixture
def dbs(tmp_path):
    """export元DB(db_a)・import先DB(db_b)の2パスを提供する(別インスタンスを模す)。"""
    db_a = str(tmp_path / "a.db")
    db_b = str(tmp_path / "b.db")
    yield db_a, db_b
    if "DISCUSSION_DB_PATH" in os.environ:
        del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture(autouse=True)
def _export_dir_under_tmp(monkeypatch, tmp_path):
    """パスガードの許可ルートをtmp_pathに向ける(export_bundle_serviceのテストと同じ作法)。"""
    monkeypatch.setattr("src.services.material_service.DEFAULT_EXPORT_DIR", str(tmp_path))


@pytest.fixture
def mock_embedding_server(monkeypatch):
    """embedding_serverへのHTTPリクエストをモック化(テキストごとに決定的なベクトルを返す)。

    query/documentのprefixに関わらずtext本体だけでシードする(実モデルはprefixが
    違ってもquery-document間の意味的近さを捉えるが、乱数モックでprefix込みシードに
    すると同一テキストのquery-documentですら別ベクトルになってしまうため、重複疑い
    検知テストの目的(完全一致に近い内容の検出)に合わせてシンプル化している)。
    """
    import src.services.embedding_service as emb

    def mock_encode_batch(texts, prefix):
        embeddings = []
        for text in texts:
            np.random.seed(hash(text) % (2**32))
            embeddings.append(np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist())
        return embeddings

    monkeypatch.setattr(emb, "_encode_batch", mock_encode_batch)
    monkeypatch.setattr(emb, "_server_initialized", True)
    monkeypatch.setattr(emb, "_backfill_done", True)
    yield


@pytest.fixture
def mock_embedding_unavailable(monkeypatch):
    """embeddingサーバー未起動(encode系がNoneを返す)状態を決定的に再現する。"""
    import src.services.embedding_service as emb

    monkeypatch.setattr(emb, "_server_initialized", True)
    monkeypatch.setattr(emb, "_backfill_done", True)
    monkeypatch.setattr(emb, "_encode_batch", lambda texts, prefix: None)
    yield


def _topic(title="Topic", tags=None):
    result = add_topic(title=title, description=f"Description for {title}", tags=tags or DEFAULT_TAGS)
    assert "error" not in result
    return result["topic_id"]


def _activity(title="Activity", tags=None):
    result = add_activity(title=title, description=f"Description for {title}", tags=tags or DEFAULT_TAGS, check_in=False)
    assert "error" not in result
    return result["activity_id"]


def _material(title="Material", content="Content", tags=None, related=None):
    result = add_material(title=title, content=content, tags=tags or DEFAULT_TAGS, source="test", related=related)
    assert "error" not in result
    return result["material_id"]


def _decision(topic_id, decision="Decision text", reason="Reason text", tags=None):
    result = add_decision(decision=decision, reason=reason, topic_id=topic_id, tags=tags or DEFAULT_TAGS)
    assert "error" not in result
    return result["decision_id"]


def _log(topic_id, content="Log content", tags=None):
    result = add_log(topic_id=topic_id, content=content, tags=tags or DEFAULT_TAGS)
    assert "error" not in result
    return result["log_id"]


def _set_instance(instance_id="team-a", force=False):
    result = set_instance_identity(instance_id, force=force)
    assert "error" not in result
    return instance_id


def _count_rows(table: str) -> int:
    conn = get_connection(load_vec=False)
    try:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    finally:
        conn.close()


class TestValidation:
    def test_invalid_mode_rejected(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_b)
        result = import_bundle("/tmp/whatever", mode="bogus")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_empty_bundle_path_rejected(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_b)
        result = import_bundle("")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_apply_rejects_unsupported_bundle_format(self, dbs, tmp_path):
        db_a, db_b = dbs
        _switch_db(db_b)
        _set_instance("team-b")
        bad_bundle = tmp_path / "bad-format-bundle"
        bad_bundle.mkdir()
        (bad_bundle / "manifest.yaml").write_text(
            "format: ccm-bundle/999\nbundle_id: x\nsource_instance: team-a\nentities: []\n",
            encoding="utf-8",
        )
        result = import_bundle(str(bad_bundle), mode="apply")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_path_outside_export_dir_rejected(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle("/etc/passwd-bundle")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_manifest_returns_not_found(self, dbs, tmp_path):
        db_a, db_b = dbs
        _switch_db(db_b)
        _set_instance("team-b")
        empty_dir = tmp_path / "empty-bundle"
        empty_dir.mkdir()
        result = import_bundle(str(empty_dir))
        assert result["error"]["code"] == "NOT_FOUND"


class TestInstanceIdGate:
    def test_import_without_instance_id_fails(self, dbs, tmp_path):
        db_a, db_b = dbs
        _switch_db(db_b)
        empty_dir = tmp_path / "some-bundle"
        empty_dir.mkdir()
        (empty_dir / "manifest.yaml").write_text("format: ccm-bundle/1\n", encoding="utf-8")
        result = import_bundle(str(empty_dir))
        assert result["error"]["code"] == "INSTANCE_ID_NOT_SET"


class TestNoSideEffects:
    def test_dry_run_does_not_write_to_db(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Sample", content="hello world")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])
        assert "error" not in bundle

        _switch_db(db_b)
        _set_instance("team-b")
        materials_before = _count_rows("materials")
        provenance_before = _count_rows("import_provenance")

        result = import_bundle(bundle["path"])
        assert "error" not in result

        assert _count_rows("materials") == materials_before
        assert _count_rows("import_provenance") == provenance_before


class TestEntityClassification:
    def test_new_material_is_classified_as_new(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Sample", content="hello world")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        assert result["format_version_ok"] is True
        assert result["bundle_id"] == bundle["bundle_id"]
        assert result["source_instance"] == "team-a"
        assert result["summary"]["material"]["new"] == 1

    def test_self_origin_entity_is_not_counted_as_new(self, dbs):
        """importする側自身が生み出したエンティティ(instance_idが自分と一致)は
        provenance行がなくても'new'ではなく'self_origin'に分類される。"""
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Roundtrip", content="came back home")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        # db_bをteam-a自身として扱う(往復シナリオの簡易再現)
        _switch_db(db_b)
        _set_instance("team-a")
        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        assert result["summary"]["material"].get("new", 0) == 0
        assert result["summary"]["material"]["self_origin"] == 1


class TestProvenanceReimport:
    def _seed_provenance(self, entity_type, entity_id, origin_instance, origin_id, content_hash, bundle_id="prior-bundle"):
        conn = get_connection(load_vec=False)
        try:
            conn.execute(
                "INSERT INTO import_provenance "
                "(entity_type, entity_id, origin_instance, origin_id, content_hash, bundle_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (entity_type, entity_id, origin_instance, origin_id, content_hash, bundle_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_matching_hash_is_unchanged(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Stable", content="unchanged content")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        import yaml

        with open(os.path.join(bundle["path"], "manifest.yaml"), encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        content_hash = manifest["entities"][0]["content_hash"]

        _switch_db(db_b)
        _set_instance("team-b")
        local_m = _material(title="Local copy", content="local")
        self._seed_provenance("material", local_m, "team-a", m1, content_hash)

        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        assert result["summary"]["material"]["unchanged"] == 1
        assert result["summary"]["material"].get("new", 0) == 0

    def test_material_hash_mismatch_is_updatable(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Changed", content="new content")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        local_m = _material(title="Local copy", content="local")
        self._seed_provenance("material", local_m, "team-a", m1, "stale-hash-does-not-match")

        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        assert result["summary"]["material"]["updatable"] == 1
        assert result["summary"]["material"].get("unchanged", 0) == 0

    def test_decision_hash_mismatch_is_upstream_changed_skip_with_warning(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        t1 = _topic("Topic")
        d1 = _decision(t1, decision="Original decision", reason="Original reason")
        bundle = export_bundle(items=[{"type": "decision", "ids": [d1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        t_local = _topic("Local Topic")
        local_d = _decision(t_local, decision="Local decision", reason="Local reason")
        self._seed_provenance("decision", local_d, "team-a", d1, "stale-hash-does-not-match")

        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        assert result["summary"]["decision"]["upstream_changed_skip"] == 1
        assert len(result["upstream_changed"]) == 1
        warning = result["upstream_changed"][0]
        assert warning["type"] == "decision"
        assert warning["local_entity_id"] == local_d


class TestDanglingRefs:
    def test_reference_outside_bundle_and_provenance_is_dangling(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Has Reference")
        m2 = _material(title="Outside Selection")
        rel = add_relation("material", m1, [{"type": "material", "ids": [m2]}], "related")
        assert "error" not in rel
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        assert result["dangling_refs"]["count"] >= 1
        assert any(f"team-a:M{m2}" == k for k in result["dangling_refs"]["sample"])

    def test_reference_resolvable_via_provenance_is_not_dangling(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Has Reference")
        m2 = _material(title="Already Imported Elsewhere")
        rel = add_relation("material", m1, [{"type": "material", "ids": [m2]}], "related")
        assert "error" not in rel
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        local_m2 = _material(title="Local copy of m2")
        conn = get_connection(load_vec=False)
        try:
            conn.execute(
                "INSERT INTO import_provenance "
                "(entity_type, entity_id, origin_instance, origin_id, content_hash, bundle_id) "
                "VALUES ('material', ?, 'team-a', ?, 'whatever-hash', 'prior-bundle')",
                (local_m2, m2),
            )
            conn.commit()
        finally:
            conn.close()

        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        assert f"team-a:M{m2}" not in result["dangling_refs"]["sample"]

    def test_belongs_to_auto_included_topic_is_resolved_within_bundle(self, dbs):
        """親topicは自動同梱されるため、decisionのbelongs_to参照はdangling判定されない。"""
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        t1 = _topic("Parent")
        d1 = _decision(t1)
        bundle = export_bundle(items=[{"type": "decision", "ids": [d1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        assert result["dangling_refs"]["count"] == 0


class TestTagReport:
    def test_unknown_tag_is_classified_as_create_with_notes(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(tags=["domain:brand-new-tag"])
        update_tag("domain:brand-new-tag", notes="How to use this tag")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        creates = {c["tag"]: c for c in result["tag_report"]["create"]}
        assert "domain:brand-new-tag" in creates
        entry = creates["domain:brand-new-tag"]
        assert entry["notes"] == "How to use this tag"
        assert entry["review_required"] is True  # domain namespace
        assert entry["incoming"]["count"] == 1

    def test_existing_tag_is_classified_as_merge_with_notes_diff(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(tags=["domain:shared-tag"])
        update_tag("domain:shared-tag", notes="incoming line one\nincoming line two")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        local_m = _material(title="Local User", tags=["domain:shared-tag"])
        update_tag("domain:shared-tag", notes="incoming line one")

        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        merges = {m["tag"]: m for m in result["tag_report"]["merge"]}
        assert "domain:shared-tag" in merges
        entry = merges["domain:shared-tag"]
        assert entry["notes_diff"] == "incoming line two"
        assert "Local User" in entry["local"]["sample_titles"]

    def test_retracted_local_entity_is_excluded_from_tag_usage_sample(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(tags=["shared-retract-tag"])
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        _material(title="Live Local User", tags=["shared-retract-tag"])
        retracted_id = _material(title="Retracted Local User", tags=["shared-retract-tag"])
        retract("material", [retracted_id])

        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        merges = {m["tag"]: m for m in result["tag_report"]["merge"]}
        entry = merges["shared-retract-tag"]
        assert entry["local"]["count"] == 1
        assert entry["local"]["sample_titles"] == ["Live Local User"]

    def test_archived_local_tag_is_classified_as_archived_hit(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(tags=["retiring-tag"])
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        _material(tags=["retiring-tag"])
        update_tag("retiring-tag", archived=True, archived_reason="superseded by new-tag")

        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        archived = {a["tag"]: a for a in result["tag_report"]["archived_hit"]}
        assert "retiring-tag" in archived
        assert archived["retiring-tag"]["archived_reason"] == "superseded by new-tag"

    def test_alias_local_tag_is_classified_as_alias_hit_with_resolved_target(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(tags=["old-name"])
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        _material(title="Local Old Name User", tags=["old-name"])
        _material(title="Local New Name User", tags=["new-name"])
        update_tag("old-name", canonical="new-name")

        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        aliases = {a["tag"]: a for a in result["tag_report"]["alias_hit"]}
        assert "old-name" in aliases
        entry = aliases["old-name"]
        assert entry["resolved_to"] == "new-name"
        # local usage は解決先canonicalタグ(new-name)に対して集計される。
        # エイリアス化によりold-name由来のjunction行もnew-name側のtag_idへ
        # 付け替わっているため、両方のtitleが含まれる。
        assert entry["local"]["count"] == 2
        assert set(entry["local"]["sample_titles"]) == {"Local Old Name User", "Local New Name User"}

    def test_plain_tag_without_notes_is_not_review_required(self, dbs):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(tags=["plain-tag"])
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        creates = {c["tag"]: c for c in result["tag_report"]["create"]}
        assert creates["plain-tag"]["review_required"] is False


class TestDuplicatesSuspected:
    def test_similar_local_material_is_flagged(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Duplicate Candidate", content="This exact content already exists locally")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        local_m = _material(
            title="Duplicate Candidate", content="This exact content already exists locally"
        )
        # backfill_embeddings相当: ローカル既存materialのembeddingを生成しておく
        from src.services.embedding_service import build_embedding_text, generate_and_store_embedding

        generate_and_store_embedding("material", local_m, build_embedding_text("Duplicate Candidate", "This exact content already exists locally"))

        result = import_bundle(bundle["path"])
        assert "error" not in result
        assert result["degraded"] is False
        assert len(result["duplicates_suspected"]) == 1
        dup = result["duplicates_suspected"][0]
        assert dup["title"] == "Duplicate Candidate"
        assert any(s["id_raw"] == local_m and s["type"] == "material" for s in dup["similar"])

    def test_degraded_when_embedding_server_unavailable(self, dbs, mock_embedding_unavailable):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Whatever", content="content")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"])
        assert "error" not in result
        assert result["degraded"] is True
        assert result["duplicates_suspected"] == []

    def test_skip_duplicate_check_bypasses_search_entirely(self, dbs, mock_embedding_unavailable):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Whatever", content="content")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], skip_duplicate_check=True)
        assert "error" not in result
        # embeddingが利用不可でも呼び出し自体をスキップするのでdegradedは立たない
        assert result["degraded"] is False

    def test_duplicate_check_sends_single_batched_encode_call(self, dbs, monkeypatch):
        """新規importエンティティが複数件あっても、クエリembeddingはHTTPリクエスト1回にまとめる。"""
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Alpha", content="alpha content")
        m2 = _material(title="Beta", content="beta content")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1, m2]}])

        _switch_db(db_b)
        _set_instance("team-b")

        import src.services.embedding_service as emb

        call_count = {"n": 0}

        def spy_encode_batch(texts, prefix):
            call_count["n"] += 1
            embeddings = []
            for text in texts:
                np.random.seed(hash(text) % (2**32))
                embeddings.append(np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist())
            return embeddings

        monkeypatch.setattr(emb, "_encode_batch", spy_encode_batch)
        monkeypatch.setattr(emb, "_server_initialized", True)
        monkeypatch.setattr(emb, "_backfill_done", True)

        result = import_bundle(bundle["path"])
        assert "error" not in result
        assert result["degraded"] is False
        assert call_count["n"] == 1


def _fetch_row(table: str, entity_id: int) -> dict:
    conn = get_connection(load_vec=False)
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _fetch_provenance(entity_type: str, entity_id: int) -> dict:
    conn = get_connection(load_vec=False)
    try:
        row = conn.execute(
            "SELECT * FROM import_provenance WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


class TestApplyEntityCreation:
    def test_apply_creates_material_with_import_time_created_at_and_provenance(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Origin Material", content="hello from team-a")
        # created_at差分をwall-clockの秒精度に依存させないため、originのcreated_atを
        # 明確に過去の固定値へ書き換えてからexportする
        origin_created_at = "2020-01-01 00:00:00"
        conn = get_connection(load_vec=False)
        try:
            conn.execute("UPDATE materials SET created_at = ? WHERE id = ?", (origin_created_at, m1))
            conn.commit()
        finally:
            conn.close()
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], mode="apply")
        assert "error" not in result
        assert result["created"]["material"] == 1
        assert result["updated"] == {}

        local_row = None
        conn = get_connection(load_vec=False)
        try:
            local_row = conn.execute(
                "SELECT id, title, content, created_at FROM materials WHERE title = ?", ("Origin Material",)
            ).fetchone()
        finally:
            conn.close()
        assert local_row is not None
        assert local_row["content"] == "hello from team-a"
        # import時刻を採用するため、origin側のcreated_atとは一致しない
        assert local_row["created_at"] != origin_created_at

        prov = _fetch_provenance("material", local_row["id"])
        assert prov["origin_instance"] == "team-a"
        assert prov["origin_id"] == m1
        assert prov["origin_created_at"] == origin_created_at
        assert prov["bundle_id"] == bundle["bundle_id"]

    def test_apply_creates_decision_with_auto_included_parent_topic_belongs_to(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        t1 = _topic("Parent Topic")
        d1 = _decision(t1, decision="We decided X", reason="Because Y")
        bundle = export_bundle(items=[{"type": "decision", "ids": [d1]}])
        assert bundle["counts"].get("topic") == 1

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], mode="apply")
        assert "error" not in result
        assert result["created"]["decision"] == 1
        assert result["created"]["topic"] == 1
        assert result["created_edges"] >= 1

        conn = get_connection(load_vec=False)
        try:
            topic_row = conn.execute(
                "SELECT id FROM discussion_topics WHERE title = ?", ("Parent Topic",)
            ).fetchone()
            decision_row = conn.execute(
                "SELECT id FROM decisions WHERE decision = ?", ("We decided X",)
            ).fetchone()
            edge = conn.execute(
                "SELECT 1 FROM relations WHERE source_type = 'decision' AND source_id = ? "
                "AND target_type = 'topic' AND target_id = ? AND relation_type = 'belongs_to'",
                (decision_row["id"], topic_row["id"]),
            ).fetchone()
        finally:
            conn.close()
        assert edge is not None

    def test_apply_preserves_activity_status_from_bundle_on_create(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        a1 = _activity(title="In Flight")
        upd = update_activity(a1, status="in_progress")
        assert "error" not in upd
        bundle = export_bundle(items=[{"type": "activity", "ids": [a1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], mode="apply")
        assert "error" not in result
        assert result["created"]["activity"] == 1

        conn = get_connection(load_vec=False)
        try:
            row = conn.execute(
                "SELECT status FROM activities WHERE title = ?", ("In Flight",)
            ).fetchone()
        finally:
            conn.close()
        # fableの推奨(未完了はshelvedに落とす)は不採用、export時点のstatusをそのまま使う
        assert row["status"] == "in_progress"


class TestApplyReferenceResolution:
    def test_apply_replaces_unresolved_body_citation_with_title(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m2 = _material(title="Outside The Bundle", content="not selected")
        m1 = _material(
            title="Has Reference", content="See details in {{cite:M#" + str(m2) + "}} above."
        )
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], mode="apply")
        assert "error" not in result
        assert result["unresolved_body_refs"] == 1

        conn = get_connection(load_vec=False)
        try:
            row = conn.execute(
                "SELECT content FROM materials WHERE title = ?", ("Has Reference",)
            ).fetchone()
        finally:
            conn.close()
        assert "「Outside The Bundle」(未取り込みの外部記録)" in row["content"]
        assert "{{cite:" not in row["content"]

    def test_apply_resolves_related_reference_within_same_bundle(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Has Reference", content="hello")
        m2 = _material(title="Referenced Together", content="world")
        rel = add_relation("material", m1, [{"type": "material", "ids": [m2]}], "related")
        assert "error" not in rel
        bundle = export_bundle(items=[{"type": "material", "ids": [m1, m2]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], mode="apply")
        assert "error" not in result
        assert result["dropped_edges"] == 0
        assert result["created_edges"] >= 1

        conn = get_connection(load_vec=False)
        try:
            row1 = conn.execute("SELECT id FROM materials WHERE title = ?", ("Has Reference",)).fetchone()
            row2 = conn.execute("SELECT id FROM materials WHERE title = ?", ("Referenced Together",)).fetchone()
            edge = conn.execute(
                "SELECT 1 FROM relations_view WHERE source_type = 'material' AND source_id = ? "
                "AND target_type = 'material' AND target_id = ? AND relation_type = 'related'",
                (row1["id"], row2["id"]),
            ).fetchone()
        finally:
            conn.close()
        assert edge is not None


class TestApplyTags:
    def test_apply_creates_new_tag_with_full_notes(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(tags=["domain:brand-new-tag"])
        update_tag("domain:brand-new-tag", notes="How to use this tag")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], mode="apply")
        assert "error" not in result

        conn = get_connection(load_vec=False)
        try:
            row = conn.execute(
                "SELECT notes FROM tags WHERE namespace = 'domain' AND name = 'brand-new-tag'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["notes"] == "How to use this tag"

    def test_apply_merges_notes_diff_into_existing_tag(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(tags=["domain:shared-tag"])
        update_tag("domain:shared-tag", notes="incoming line one\nincoming line two")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        _material(title="Local User", tags=["domain:shared-tag"])
        update_tag("domain:shared-tag", notes="incoming line one")

        result = import_bundle(bundle["path"], mode="apply")
        assert "error" not in result

        conn = get_connection(load_vec=False)
        try:
            row = conn.execute(
                "SELECT notes FROM tags WHERE namespace = 'domain' AND name = 'shared-tag'"
            ).fetchone()
        finally:
            conn.close()
        assert row["notes"] == "incoming line one\n\nincoming line two"

    def test_apply_tag_renames_resolution_redirects_to_local_tag(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(tags=["domain:api"])
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(
            bundle["path"],
            mode="apply",
            resolutions={"tag_renames": {"domain:api": "domain:teama-api"}},
        )
        assert "error" not in result

        conn = get_connection(load_vec=False)
        try:
            renamed = conn.execute(
                "SELECT id FROM tags WHERE namespace = 'domain' AND name = 'teama-api'"
            ).fetchone()
            original = conn.execute(
                "SELECT id FROM tags WHERE namespace = 'domain' AND name = 'api'"
            ).fetchone()
            material_row = conn.execute("SELECT id FROM materials WHERE title = 'Material'").fetchone()
            linked = conn.execute(
                "SELECT 1 FROM material_tags WHERE material_id = ? AND tag_id = ?",
                (material_row["id"], renamed["id"] if renamed else -1),
            ).fetchone()
        finally:
            conn.close()
        assert renamed is not None
        assert original is None
        assert linked is not None


class TestApplySearchIndexing:
    def test_apply_created_material_is_findable_via_fts_search(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Zzyzx Searchable Marker", content="unique searchable content")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], mode="apply")
        assert "error" not in result

        search_result = search(keyword="Zzyzx", entity_type="material")
        assert "error" not in search_result
        titles = [r["title"] for r in search_result["results"]]
        assert "Zzyzx Searchable Marker" in titles

    def test_apply_created_material_has_vec_index_row(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Vector Indexed Material", content="content for embedding")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        result = import_bundle(bundle["path"], mode="apply")
        assert "error" not in result

        conn = get_connection()
        try:
            local_id = conn.execute(
                "SELECT id FROM materials WHERE title = ?", ("Vector Indexed Material",)
            ).fetchone()["id"]
            search_index_id = conn.execute(
                "SELECT id FROM search_index WHERE source_type = 'material' AND source_id = ?",
                (local_id,),
            ).fetchone()["id"]
            vec_row = conn.execute(
                "SELECT 1 FROM vec_index WHERE rowid = ?", (search_index_id,)
            ).fetchone()
        finally:
            conn.close()
        assert vec_row is not None


class TestApplyReimport:
    def test_apply_second_run_with_unchanged_hash_is_all_skipped(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Stable Content", content="does not change")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        first = import_bundle(bundle["path"], mode="apply")
        assert "error" not in first
        assert first["created"]["material"] == 1

        materials_after_first = _count_rows("materials")
        second = import_bundle(bundle["path"], mode="apply")
        assert "error" not in second
        assert second["created"] == {}
        assert second["skipped"]["material"] == 1
        assert second["skip_reasons"].get("unchanged") == 1
        assert _count_rows("materials") == materials_after_first

    def test_apply_on_upstream_change_overwrite_updates_decision(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        t1 = _topic("Topic")
        d1 = _decision(t1, decision="Original decision", reason="Original reason")
        bundle = export_bundle(items=[{"type": "decision", "ids": [d1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        t_local = _topic("Local Topic")
        local_d = _decision(t_local, decision="Local decision", reason="Local reason")
        conn = get_connection(load_vec=False)
        try:
            conn.execute(
                "INSERT INTO import_provenance "
                "(entity_type, entity_id, origin_instance, origin_id, content_hash, bundle_id) "
                "VALUES ('decision', ?, 'team-a', ?, 'stale-hash-does-not-match', 'prior-bundle')",
                (local_d, d1),
            )
            conn.commit()
        finally:
            conn.close()

        result = import_bundle(
            bundle["path"],
            mode="apply",
            resolutions={"on_upstream_change": {"decision": "overwrite"}},
        )
        assert "error" not in result
        assert result["updated"]["decision"] == 1

        row = _fetch_row("decisions", local_d)
        assert row["decision"] == "Original decision"
        assert row["reason"] == "Original reason"


class TestApplyEntityOverrides:
    def test_apply_entity_override_skip_excludes_entity(self, dbs, mock_embedding_server):
        db_a, db_b = dbs
        _switch_db(db_a)
        _set_instance("team-a")
        m1 = _material(title="Skip Me", content="should not be imported")
        bundle = export_bundle(items=[{"type": "material", "ids": [m1]}])

        _switch_db(db_b)
        _set_instance("team-b")
        key = f"team-a:M{m1}"
        result = import_bundle(
            bundle["path"],
            mode="apply",
            resolutions={"entity_overrides": {key: "skip"}},
        )
        assert "error" not in result
        assert result["created"] == {}
        assert result["skipped"]["material"] == 1
        assert result["skip_reasons"].get("explicit_override") == 1

        conn = get_connection(load_vec=False)
        try:
            row = conn.execute("SELECT id FROM materials WHERE title = ?", ("Skip Me",)).fetchone()
        finally:
            conn.close()
        assert row is None
