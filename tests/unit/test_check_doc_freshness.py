"""scripts/check_doc_freshness.py のユニットテスト。"""
import os
import tempfile
from pathlib import Path

import pytest

from src.db import get_connection, init_database
from src.services.tag_service import _injected_tags
from src.services.topic_service import add_topic
from tests.helpers import add_decision

from scripts.check_doc_freshness import (
    DocMarker,
    check_doc,
    count_new_direction_events,
    count_new_tagged_decisions,
    find_marked_docs,
    max_migration_number,
    parse_marker,
    run,
)


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _tag_decision(decision_id: int, namespace: str, name: str) -> None:
    """decisionにタグを直付けする（tag_serviceのnamespace検証を経由しない、テスト専用のraw insert）。

    layer:direction はこのリポジトリではまだ有効なnamespaceとして導入されていない
    （方向性層コンポーネントの管轄）ため、checkerのDB直接クエリ挙動をテストする目的で
    add_decisions 経由ではなく直接 tags/decision_tags に書き込む。
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM tags WHERE namespace = ? AND name = ?", (namespace, name)
        ).fetchone()
        if row:
            tag_id = row[0]
        else:
            conn.execute(
                "INSERT INTO tags (namespace, name) VALUES (?, ?)", (namespace, name)
            )
            tag_id = conn.execute(
                "SELECT id FROM tags WHERE namespace = ? AND name = ?", (namespace, name)
            ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO decision_tags (decision_id, tag_id) VALUES (?, ?)",
            (decision_id, tag_id),
        )
        conn.commit()
    finally:
        conn.close()


def _add_supersede(source_id: int, target_id: int, created_at: str | None = None) -> None:
    conn = get_connection()
    try:
        if created_at is not None:
            conn.execute(
                "INSERT INTO decision_supersedes (source_id, target_id, created_at) VALUES (?, ?, ?)",
                (source_id, target_id, created_at),
            )
        else:
            conn.execute(
                "INSERT INTO decision_supersedes (source_id, target_id) VALUES (?, ?)",
                (source_id, target_id),
            )
        conn.commit()
    finally:
        conn.close()


def _set_created_at(table: str, entity_id: int, created_at: str) -> None:
    conn = get_connection()
    try:
        conn.execute(f"UPDATE {table} SET created_at = ? WHERE id = ?", (created_at, entity_id))
        conn.commit()
    finally:
        conn.close()


# --- parse_marker ---


def test_parse_marker_extracts_all_fields():
    text = """# doc

<!-- ccm-doc-sync
watch-tags: domain:cc-memory, domain:ow
watch-direction: true
watch-migrations: true
last-synced: 2026-07-01
last-synced-migration: 0048
-->

本文
"""
    marker = parse_marker(text, Path("dummy.md"))
    assert marker is not None
    assert marker.watch_tags == ["domain:cc-memory", "domain:ow"]
    assert marker.watch_direction is True
    assert marker.watch_migrations is True
    assert marker.last_synced == "2026-07-01"
    assert marker.last_synced_migration == "0048"


def test_parse_marker_returns_none_without_marker():
    assert parse_marker("# ただのdoc\n本文のみ", Path("dummy.md")) is None


def test_parse_marker_defaults_when_fields_omitted():
    text = "<!-- ccm-doc-sync\nlast-synced: 2026-01-01\n-->"
    marker = parse_marker(text, Path("dummy.md"))
    assert marker is not None
    assert marker.watch_tags == []
    assert marker.watch_direction is False
    assert marker.watch_migrations is False
    assert marker.last_synced_migration is None


# --- find_marked_docs ---


def test_find_marked_docs_scans_docs_root_and_explicit(tmp_path):
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    (docs_root / "a.md").write_text("a")
    nested = docs_root / "sub"
    nested.mkdir()
    (nested / "b.md").write_text("b")
    explicit = tmp_path / "OUTSIDE.md"
    explicit.write_text("outside")

    found = find_marked_docs(docs_root, [explicit])
    assert explicit in found
    assert (docs_root / "a.md") in found
    assert (nested / "b.md") in found


# --- count_new_tagged_decisions ---


def test_count_new_tagged_decisions_counts_only_after_last_synced(temp_db):
    topic = add_topic(
        title="t", description="d", tags=["domain:doc-sync-test"]
    )
    topic_id = topic["topic_id"]

    old = add_decision("old decision", "reason", topic_id, tags=["domain:doc-sync-test"])
    new = add_decision("new decision", "reason", topic_id, tags=["domain:doc-sync-test"])
    _set_created_at("decisions", old["decision_id"], "2026-01-01 00:00:00")
    _set_created_at("decisions", new["decision_id"], "2026-06-01 00:00:00")

    conn = get_connection()
    try:
        n = count_new_tagged_decisions(conn, "domain:doc-sync-test", "2026-03-01")
    finally:
        conn.close()
    assert n == 1


def test_count_new_tagged_decisions_zero_for_unknown_tag(temp_db):
    conn = get_connection()
    try:
        n = count_new_tagged_decisions(conn, "domain:does-not-exist", None)
    finally:
        conn.close()
    assert n == 0


def test_count_new_tagged_decisions_excludes_retracted(temp_db):
    from src.services.retract_service import retract

    topic = add_topic(title="t", description="d", tags=["domain:doc-sync-retract"])
    topic_id = topic["topic_id"]
    d = add_decision("will retract", "reason", topic_id, tags=["domain:doc-sync-retract"])
    _set_created_at("decisions", d["decision_id"], "2026-06-01 00:00:00")
    retract("decision", [d["decision_id"]])

    conn = get_connection()
    try:
        n = count_new_tagged_decisions(conn, "domain:doc-sync-retract", "2026-01-01")
    finally:
        conn.close()
    assert n == 0


def test_count_new_tagged_decisions_via_topic_tag_inheritance(temp_db):
    """decisionに直付けタグが無くても、親topicのtopic_tags継承でカウントされる。"""
    topic = add_topic(title="t", description="d", tags=["domain:doc-sync-inherit"])
    topic_id = topic["topic_id"]
    d = add_decision("no direct tag", "reason", topic_id, tags=[])
    _set_created_at("decisions", d["decision_id"], "2026-06-01 00:00:00")

    conn = get_connection()
    try:
        n = count_new_tagged_decisions(conn, "domain:doc-sync-inherit", "2026-01-01")
    finally:
        conn.close()
    assert n == 1


# --- count_new_direction_events ---


def test_count_new_direction_events_counts_new_direction_decision(temp_db):
    topic = add_topic(title="t", description="d", tags=["domain:direction-test"])
    topic_id = topic["topic_id"]
    d = add_decision("direction decision", "reason", topic_id, tags=[])
    _tag_decision(d["decision_id"], "layer", "direction")
    _set_created_at("decisions", d["decision_id"], "2026-06-01 00:00:00")

    conn = get_connection()
    try:
        n = count_new_direction_events(conn, "2026-01-01")
    finally:
        conn.close()
    assert n == 1


def test_count_new_direction_events_counts_supersede_regardless_of_decision_created_at(temp_db):
    """supersedeイベントはdecision_supersedes.created_atで判定する（decision自体のcreated_atではない）。"""
    topic = add_topic(title="t", description="d", tags=["domain:direction-test2"])
    topic_id = topic["topic_id"]
    old_direction = add_decision("old direction", "reason", topic_id, tags=[])
    new_direction = add_decision("new direction", "reason", topic_id, tags=[])
    _tag_decision(old_direction["decision_id"], "layer", "direction")
    _tag_decision(new_direction["decision_id"], "layer", "direction")
    # 両方とも古いdecisionだが、supersede自体は最近発生した
    _set_created_at("decisions", old_direction["decision_id"], "2025-01-01 00:00:00")
    _set_created_at("decisions", new_direction["decision_id"], "2025-01-01 00:00:00")
    _add_supersede(new_direction["decision_id"], old_direction["decision_id"], "2026-06-01 00:00:00")

    conn = get_connection()
    try:
        n = count_new_direction_events(conn, "2026-01-01")
    finally:
        conn.close()
    assert n == 1


def test_count_new_direction_events_zero_when_no_direction_tag(temp_db):
    conn = get_connection()
    try:
        n = count_new_direction_events(conn, None)
    finally:
        conn.close()
    assert n == 0


# --- max_migration_number ---


def test_max_migration_number_handles_duplicates(tmp_path):
    for name in ["0001_a.sql", "0005_a.sql", "0005_b.sql", "0012_c.sql"]:
        (tmp_path / name).write_text("-- sql")
    assert max_migration_number(tmp_path) == 12


def test_max_migration_number_empty_dir_returns_none(tmp_path):
    assert max_migration_number(tmp_path) is None


# --- check_doc / run integration ---


def test_check_doc_watch_migrations_stale(tmp_path, temp_db):
    for name in ["0001_a.sql", "0002_b.sql", "0050_c.sql"]:
        (tmp_path / name).write_text("-- sql")
    marker = DocMarker(
        path=Path("dummy.md"),
        watch_migrations=True,
        last_synced_migration="0002",
    )
    conn = get_connection()
    try:
        reasons = check_doc(conn, tmp_path, marker)
    finally:
        conn.close()
    assert any("0003..0050" in r for r in reasons)


def test_check_doc_watch_migrations_fresh_when_synced(tmp_path, temp_db):
    for name in ["0001_a.sql", "0002_b.sql"]:
        (tmp_path / name).write_text("-- sql")
    marker = DocMarker(
        path=Path("dummy.md"),
        watch_migrations=True,
        last_synced_migration="0002",
    )
    conn = get_connection()
    try:
        reasons = check_doc(conn, tmp_path, marker)
    finally:
        conn.close()
    assert reasons == []


def test_run_skips_docs_without_marker_and_flags_stale_doc(tmp_path, temp_db):
    docs_root = tmp_path / "docs"
    docs_root.mkdir()

    (docs_root / "no_marker.md").write_text("# 何もないdoc")

    topic = add_topic(title="t", description="d", tags=["domain:run-test"])
    topic_id = topic["topic_id"]
    d = add_decision("fresh decision", "reason", topic_id, tags=["domain:run-test"])
    _set_created_at("decisions", d["decision_id"], "2026-06-01 00:00:00")

    stale_doc = docs_root / "stale.md"
    stale_doc.write_text(
        "<!-- ccm-doc-sync\n"
        "watch-tags: domain:run-test\n"
        "last-synced: 2026-01-01\n"
        "-->\n# stale doc\n"
    )

    result = run(docs_root, [], Path(os.environ["DISCUSSION_DB_PATH"]))

    doc_paths = {r["doc"] for r in result["docs"]}
    assert str(docs_root / "no_marker.md") not in doc_paths
    assert str(stale_doc) in doc_paths
    assert result["stale"] is True
    stale_entry = next(r for r in result["docs"] if r["doc"] == str(stale_doc))
    assert stale_entry["stale"] is True
    assert len(stale_entry["reasons"]) == 1
