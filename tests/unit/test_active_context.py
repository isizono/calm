"""_build_activities_section および関連ヘルパー関数のユニットテスト

データ取得関数はsrc/services/activity_service.pyに、
表示整形関数はhooks/session_start_hook.pyに配置されている。
"""
import os
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.activity_service import (
    add_activity,
    update_activity,
    get_active_domains,
    get_active_activities_by_tag,
    get_pinned_active_activities,
)
import src.services.embedding_service as emb
from tests.helpers import add_decision
from hooks.session_start_hook import (
    _build_activities_section,
    _calc_elapsed_days,
    _DETERMINISTIC_RENDER_NOTICE,
    _TIER2_MAX_ITEMS,
    _TIER4_STALE_DAYS,
)


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


def _get_tag_id(namespace: str, name: str) -> int:
    """テスト用: タグIDを取得する"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM tags WHERE namespace = ? AND name = ?",
            (namespace, name),
        ).fetchone()
        return row["id"] if row else -1
    finally:
        conn.close()


def _build_active_context_wrapper():
    """テスト用: connを自動管理してアクティビティセクションを組み立てる"""
    conn = get_connection()
    try:
        return _build_activities_section(conn)
    finally:
        conn.close()


def _age_activities(hours: int = 48) -> None:
    """全アクティビティの created_at / updated_at を指定時間前に書き戻す。

    階層 3 (created_at 24h) から外し、階層 4 (updated_at 30d) に落とすためのヘルパ。
    """
    conn = get_connection()
    try:
        past = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn.execute(
            "UPDATE activities SET created_at = ?, updated_at = ?", (past, past)
        )
        conn.commit()
    finally:
        conn.close()


def test_deterministic_render_notice_constant():
    """末尾固定文の文言が仕様通りである"""
    assert "決定論的に組み立てた表示用 markdown" in _DETERMINISTIC_RENDER_NOTICE
    assert "再フォーマットや優先順の再評価をせず" in _DETERMINISTIC_RENDER_NOTICE


def test_tier2_max_items_constant():
    """階層 2 の上限は 5"""
    assert _TIER2_MAX_ITEMS == 5


def test_tier4_stale_days_constant():
    """階層 4 の updated_at 上限は 30 日"""
    assert _TIER4_STALE_DAYS == 30


def test_calc_elapsed_days_today():
    now = datetime.now(timezone.utc).isoformat()
    assert _calc_elapsed_days(now) == 0


def test_calc_elapsed_days_3_days_ago():
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert _calc_elapsed_days(three_days_ago) == 3


def test_calc_elapsed_days_sqlite_format():
    assert _calc_elapsed_days("2026-03-14 10:00:00") >= 0


def test_calc_elapsed_days_invalid_string():
    assert _calc_elapsed_days("not-a-date") == 0


def test_calc_elapsed_days_none():
    assert _calc_elapsed_days(None) == 0


def test_calc_elapsed_days_empty():
    assert _calc_elapsed_days("") == 0


def test_get_active_domains_with_active_activity(temp_db):
    add_activity(
        title="Activity 1", description="Desc",
        tags=["domain:myproject"], check_in=False,
    )
    domains = get_active_domains()
    names = [d["name"] for d in domains]
    assert "myproject" in names


def test_get_active_domains_excludes_completed(temp_db):
    result = add_activity(
        title="Done", description="Desc",
        tags=["domain:completed-proj"], check_in=False,
    )
    update_activity(result["activity_id"], status="completed")
    domains = get_active_domains()
    names = [d["name"] for d in domains]
    assert "completed-proj" not in names


def test_get_active_domains_excludes_non_domain(temp_db):
    add_activity(
        title="Activity 1", description="Desc",
        tags=["intent:design"], check_in=False,
    )
    domains = get_active_domains()
    names = [d["name"] for d in domains]
    assert "design" not in names


def test_get_active_domains_sorted_by_name(temp_db):
    add_activity(title="Z", description="Desc", tags=["domain:zzz"], check_in=False)
    add_activity(title="A", description="Desc", tags=["domain:aaa"], check_in=False)
    domains = get_active_domains()
    names = [d["name"] for d in domains]
    assert names.index("aaa") < names.index("zzz")


def test_get_active_domains_deduplicates(temp_db):
    add_activity(title="A1", description="Desc", tags=["domain:myproject"], check_in=False)
    add_activity(title="A2", description="Desc", tags=["domain:myproject"], check_in=False)
    domains = get_active_domains()
    assert len([d for d in domains if d["name"] == "myproject"]) == 1


def test_get_active_domains_no_activities(temp_db):
    add_topic(title="Topic Only", description="Desc", tags=["domain:topic-only-proj"])
    domains = get_active_domains()
    names = [d["name"] for d in domains]
    assert "topic-only-proj" not in names


def test_get_active_activities_by_tag_basic(temp_db):
    add_activity(title="Activity 1", description="Desc", tags=["domain:test-proj"], check_in=False)
    tag_id = _get_tag_id("domain", "test-proj")
    activities = get_active_activities_by_tag(tag_id)
    assert len(activities) == 1
    assert activities[0]["title"] == "Activity 1"
    assert activities[0]["status"] == "pending"


def test_get_active_activities_by_tag_has_updated_at(temp_db):
    add_activity(title="Activity 1", description="Desc", tags=["domain:test-proj"], check_in=False)
    tag_id = _get_tag_id("domain", "test-proj")
    activities = get_active_activities_by_tag(tag_id)
    assert "updated_at" in activities[0]
    assert activities[0]["updated_at"] is not None


def test_get_active_activities_by_tag_excludes_completed(temp_db):
    result = add_activity(title="Done Activity", description="Desc", tags=["domain:test-proj"], check_in=False)
    update_activity(result["activity_id"], status="completed")
    tag_id = _get_tag_id("domain", "test-proj")
    activities = get_active_activities_by_tag(tag_id)
    assert len(activities) == 0


def test_get_active_activities_by_tag_sort_order(temp_db):
    r1 = add_activity(title="Pending Activity", description="Desc", tags=["domain:test-proj"], check_in=False)
    r2 = add_activity(title="In Progress Activity", description="Desc", tags=["domain:test-proj"], check_in=False)
    update_activity(r2["activity_id"], status="in_progress")
    tag_id = _get_tag_id("domain", "test-proj")
    activities = get_active_activities_by_tag(tag_id)
    assert len(activities) == 2
    assert activities[0]["status"] == "in_progress"
    assert activities[1]["status"] == "pending"


def test_get_active_activities_by_tag_empty(temp_db):
    add_topic(title="Topic Only", description="Desc", tags=["domain:no-activities"])
    tag_id = _get_tag_id("domain", "no-activities")
    activities = get_active_activities_by_tag(tag_id)
    assert activities == []


def _pin_activity(activity_id: int) -> None:
    """テスト用: pins テーブルに activity 宛の pin を挿入する"""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO pins (source_type, source_id, target_type, target_id) "
            "VALUES ('topic', 1, 'activity', ?)",
            (activity_id,),
        )
        conn.commit()
    finally:
        conn.close()


def test_get_pinned_active_activities_returns_pinned(temp_db):
    result = add_activity(
        title="Pinned Activity", description="Desc",
        tags=["domain:pinproj"], check_in=False,
    )
    _pin_activity(result["activity_id"])
    pinned = get_pinned_active_activities()
    ids = [a["id"] for a in pinned]
    assert result["activity_id"] in ids


def test_get_pinned_active_activities_excludes_unpinned(temp_db):
    add_activity(
        title="Unpinned Activity", description="Desc",
        tags=["domain:pinproj"], check_in=False,
    )
    pinned = get_pinned_active_activities()
    titles = [a["title"] for a in pinned]
    assert "Unpinned Activity" not in titles


def test_get_pinned_active_activities_excludes_completed(temp_db):
    result = add_activity(
        title="Done Pinned", description="Desc",
        tags=["domain:pinproj"], check_in=False,
    )
    _pin_activity(result["activity_id"])
    update_activity(result["activity_id"], status="completed")
    pinned = get_pinned_active_activities()
    ids = [a["id"] for a in pinned]
    assert result["activity_id"] not in ids


def test_get_pinned_active_activities_empty(temp_db):
    add_activity(
        title="Plain Activity", description="Desc",
        tags=["domain:pinproj"], check_in=False,
    )
    assert get_pinned_active_activities() == []


def test_build_activities_section_empty(temp_db):
    """アクティブなアクティビティがない場合は空文字列"""
    result = _build_active_context_wrapper()
    assert result == ""


def test_build_activities_section_empty_with_only_topics(temp_db):
    """トピックだけでアクティビティがない場合も空文字列"""
    add_topic(title="Topic Only", description="Desc", tags=["domain:myapp"])
    result = _build_active_context_wrapper()
    assert result == ""


def test_build_activities_section_with_activities(temp_db):
    """階層 4 でアクティビティが topic 別グルーピング（topic 無しは『その他』）で表示される"""
    add_activity(title="[作業] 実装する", description="Desc", tags=["domain:myapp"], check_in=False)
    _age_activities()

    result = _build_active_context_wrapper()

    assert "# アクティビティ一覧" in result
    assert "## その他" in result
    assert "[作業] 実装する" in result


def test_build_activities_section_status_marker_pending_in_recent_tier(temp_db):
    """階層 3 の pending アクティビティに○マーカーが付く"""
    add_activity(title="[作業] 実装する", description="Desc", tags=["domain:myapp"], check_in=False)

    result = _build_active_context_wrapper()

    assert "○" in result


def test_build_activities_section_status_marker_in_progress(temp_db):
    """in_progress アクティビティは階層 2 で●マーカーが付く"""
    r = add_activity(title="[作業] 実装する", description="Desc", tags=["domain:myapp"], check_in=False)
    update_activity(r["activity_id"], status="in_progress")

    result = _build_active_context_wrapper()

    assert "●" in result


def test_build_activities_section_elapsed_days_in_title_line(temp_db):
    """経過日数はタイトル行末尾に `(Nd)` として付く"""
    add_activity(title="[作業] 実装する", description="Desc", tags=["domain:myapp"], check_in=False)

    result = _build_active_context_wrapper()

    assert "(0d)" in result


def test_build_activities_section_no_meta_updated_line(temp_db):
    """`updated: Nd ago` の meta 行は出力されない"""
    add_activity(title="[作業] 実装する", description="Desc", tags=["domain:myapp"], check_in=False)

    result = _build_active_context_wrapper()

    assert "updated:" not in result
    assert "d ago" not in result


def test_build_activities_section_no_topic_section(temp_db):
    """旧トピックセクション（最新トピック:）は出力されない"""
    add_topic(title="My Topic", description="Desc", tags=["domain:myapp"])
    add_activity(title="[作業] 実装する", description="Desc", tags=["domain:myapp"], check_in=False)

    result = _build_active_context_wrapper()

    assert "最新トピック:" not in result
    assert "My Topic" not in result


def test_build_activities_section_no_recent_tags_section(temp_db):
    """最近使われたタグセクションが出力されない"""
    add_topic(title="Topic", description="Desc", tags=["domain:myapp", "intent:design", "hooks"])
    add_activity(title="[作業] 実装する", description="Desc", tags=["domain:myapp"], check_in=False)

    result = _build_active_context_wrapper()

    assert "## 最近使われたタグ" not in result


def test_build_activities_section_tier2_capped_at_five(temp_db):
    """階層 2『優先』は上位 5 件までに絞られる"""
    for i in range(7):
        r = add_activity(
            title=f"[作業] Activity {i}", description="Desc",
            tags=["domain:myapp"], check_in=False,
        )
        update_activity(r["activity_id"], status="in_progress")
    _age_activities()

    result = _build_active_context_wrapper()

    idx_tier2 = result.index("## 優先")
    next_section = result.find("\n## ", idx_tier2 + 1)
    tier2_block = result[idx_tier2:] if next_section == -1 else result[idx_tier2:next_section]
    for expected in ("1. ●", "2. ●", "3. ●", "4. ●", "5. ●"):
        assert expected in tier2_block, f"番号 '{expected}' が階層 2 に無い"
    assert "6. ●" not in tier2_block
    assert "6. ○" not in tier2_block


def test_build_activities_section_numbered_list(temp_db):
    """階層 2/3 で連番が振られる"""
    add_activity(title="First", description="Desc", tags=["domain:myapp"], check_in=False)
    add_activity(title="Second", description="Desc", tags=["domain:myapp"], check_in=False)

    result = _build_active_context_wrapper()

    assert "1. " in result
    assert "2. " in result


def test_build_activities_section_no_total_count_line(temp_db):
    """全N件の合計行は出力されない（新仕様で撤去）"""
    for i in range(3):
        add_activity(
            title=f"[作業] Activity {i}", description="Desc",
            tags=["domain:myapp"], check_in=False,
        )
    _age_activities()

    result = _build_active_context_wrapper()

    assert "全3件" not in result
    assert "全" not in result or "全件" not in result


def test_build_activities_section_domain_with_zero_activities_skipped(temp_db):
    """アクティビティ0件のdomainセクションはスキップ"""
    add_activity(title="Activity", description="Desc", tags=["domain:myapp"], check_in=False)

    conn = get_connection()
    try:
        from src.services.tag_service import ensure_tag_ids
        ensure_tag_ids(conn, [("domain", "empty-domain")])
        conn.commit()
    finally:
        conn.close()

    result = _build_active_context_wrapper()

    assert "Activity" in result
    assert "empty-domain" not in result


def test_build_activities_section_multiple_domains_grouped(temp_db):
    """複数 domain の pending アクティビティは階層 4 の『その他』に集約される"""
    add_activity(title="App Activity", description="Desc", tags=["domain:app"], check_in=False)
    add_activity(title="Lib Activity", description="Desc", tags=["domain:lib"], check_in=False)
    _age_activities()

    result = _build_active_context_wrapper()

    assert "App Activity" in result
    assert "Lib Activity" in result
    assert "## その他" in result


def test_build_activities_section_activity_id_in_bracket(temp_db):
    """アクティビティIDが「title (#NNN)」形式で表示される"""
    activity = add_activity(title="Activity 1", description="Desc", tags=["domain:myapp"], check_in=False)
    activity_id = activity["activity_id"]

    result = _build_active_context_wrapper()

    assert f"Activity 1 (#{activity_id})" in result


def test_build_activities_section_raises_on_invalid_db(temp_db):
    """DB接続失敗時は例外が発生する（hookのmain()がcatchする前提）"""
    os.environ["DISCUSSION_DB_PATH"] = "/nonexistent/path/test.db"

    with pytest.raises(Exception):
        _build_active_context_wrapper()

    os.environ["DISCUSSION_DB_PATH"] = temp_db


def test_build_activities_section_completed_activities_excluded(temp_db):
    """completedアクティビティは表示されない"""
    result = add_activity(title="Done Activity", description="Desc", tags=["domain:myapp"], check_in=False)
    update_activity(result["activity_id"], status="completed")

    ctx = _build_active_context_wrapper()

    assert "Done Activity" not in ctx


def test_build_activities_section_deterministic_render_notice(temp_db):
    """通常アクティビティが1件以上あれば末尾固定文が末尾に付く"""
    add_activity(title="[作業] Task", description="Desc", tags=["domain:myapp"], check_in=False)
    _age_activities()

    result = _build_active_context_wrapper()

    assert "決定論的に組み立てた表示用 markdown" in result


def test_build_activities_section_no_scoring_instructions(temp_db):
    """旧スコアリング指示文は出力されない"""
    add_activity(title="[作業] Task", description="Desc", tags=["domain:myapp"], check_in=False)
    _age_activities()

    result = _build_active_context_wrapper()

    assert "# スコアリング指示" not in result
    assert "上位5件を選び" not in result
    assert "depends_on未完了" not in result


def test_build_activities_section_no_tags_metadata(temp_db):
    """新仕様では tags meta 行は出力されない"""
    topic = add_topic(title="t", description="d", tags=["domain:myapp"])
    dec = add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
    add_activity(
        title="[作業] Task", description="Desc",
        tags=["domain:myapp", "intent:implement"],
        related=[{"type": "decision", "ids": [dec["decision_id"]]}],
        check_in=False,
    )

    result = _build_active_context_wrapper()

    assert "tags:" not in result


def test_build_activities_section_no_description_snippet(temp_db):
    """新仕様では desc snippet 行は出力されない"""
    add_activity(
        title="[作業] Task", description="締め切りは来週金曜日",
        tags=["domain:myapp"], check_in=False,
    )

    result = _build_active_context_wrapper()

    assert "desc:" not in result
    assert "締め切り" not in result


def test_build_activities_section_blocked_by_meta_shown(temp_db):
    """未完了の依存先がある in_progress activity には blocked_by meta 行が付く"""
    r1 = add_activity(title="Dependency Task", description="Desc", tags=["domain:myapp"], check_in=False)
    r2 = add_activity(title="Blocked Task", description="Desc", tags=["domain:myapp"], check_in=False)
    update_activity(r2["activity_id"], status="in_progress")

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
            (r2["activity_id"], r1["activity_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    result = _build_active_context_wrapper()

    assert "blocked_by:" in result
    assert "Dependency Task" in result


def test_build_activities_section_no_blocked_by_when_dep_completed(temp_db):
    """依存先がcompletedの場合、blocked_byは表示されない"""
    r1 = add_activity(title="Completed Dep", description="Desc", tags=["domain:myapp"], check_in=False)
    r2 = add_activity(title="Unblocked Task", description="Desc", tags=["domain:myapp"], check_in=False)
    update_activity(r1["activity_id"], status="completed")

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
            (r2["activity_id"], r1["activity_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    result = _build_active_context_wrapper()

    assert "blocked_by:" not in result


def test_build_activities_section_deduplicates_multi_domain(temp_db):
    """複数domainに属するアクティビティは1回だけ表示される"""
    r = add_activity(
        title="Multi Domain Task", description="Desc",
        tags=["domain:app", "domain:lib"], check_in=False,
    )
    aid = r["activity_id"]

    result = _build_active_context_wrapper()

    assert result.count(f"(#{aid})") == 1


def test_build_activities_section_tier2_flat_no_topic_grouping(temp_db):
    """階層 2『優先』は flat リストで、topic 見出しを持たない"""
    topic_a = add_topic(title="TopicA", description="d", tags=["domain:myapp"])
    r1 = add_activity(
        title="[議論] stop_hookのスキップ機能", description="機能の設計",
        tags=["domain:myapp"],
        related=[{"type": "topic", "ids": [topic_a["topic_id"]]}],
        check_in=False,
    )
    update_activity(r1["activity_id"], status="in_progress")

    result = _build_active_context_wrapper()

    idx_tier2 = result.index("## 優先")
    next_section = result.find("\n## ", idx_tier2 + 1)
    tier2_block = result[idx_tier2:] if next_section == -1 else result[idx_tier2:next_section]

    assert "## TopicA" not in tier2_block
    assert "1. ●" in tier2_block
    assert "[議論] stop_hookのスキップ機能" in tier2_block
