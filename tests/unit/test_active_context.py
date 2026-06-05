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
)
import src.services.embedding_service as emb
from hooks.session_start_hook import (
    _build_activities_section,
    _calc_elapsed_days,
    _SCORING_INSTRUCTIONS,
    _DESCRIPTION_SNIPPET_LENGTH,
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


def _age_all_activities(hours: int = 48) -> None:
    """スコアリング対象セクション検証用: 全アクティビティのcreated_atを指定時間前に書き戻す。"""
    conn = get_connection()
    try:
        past = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn.execute("UPDATE activities SET created_at = ?", (past,))
        conn.commit()
    finally:
        conn.close()


# ========================================
# 定数の確認
# ========================================


def test_description_snippet_length():
    """description切り出し文字数が100"""
    assert _DESCRIPTION_SNIPPET_LENGTH == 100


# ========================================
# _calc_elapsed_days のテスト
# ========================================


def test_calc_elapsed_days_today():
    """本日更新なら0日"""
    now = datetime.now(timezone.utc).isoformat()
    assert _calc_elapsed_days(now) == 0


def test_calc_elapsed_days_3_days_ago():
    """3日前なら3"""
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert _calc_elapsed_days(three_days_ago) == 3


def test_calc_elapsed_days_sqlite_format():
    """SQLiteのCURRENT_TIMESTAMP形式（スペース区切り、TZ情報なし）でも正しく計算"""
    assert _calc_elapsed_days("2026-03-14 10:00:00") >= 0


def test_calc_elapsed_days_invalid_string():
    """不正な文字列なら0"""
    assert _calc_elapsed_days("not-a-date") == 0


def test_calc_elapsed_days_none():
    """Noneなら0"""
    assert _calc_elapsed_days(None) == 0


def test_calc_elapsed_days_empty():
    """空文字列なら0"""
    assert _calc_elapsed_days("") == 0


# ========================================
# get_active_domains のテスト
# ========================================


def test_get_active_domains_with_active_activity(temp_db):
    """アクティブなアクティビティがあるdomainが返る"""
    add_activity(
        title="Activity 1", description="Desc",
        tags=["domain:myproject"], check_in=False,
    )

    domains = get_active_domains()
    names = [d["name"] for d in domains]
    assert "myproject" in names


def test_get_active_domains_excludes_completed(temp_db):
    """completedアクティビティのみのdomainは返らない"""
    result = add_activity(
        title="Done", description="Desc",
        tags=["domain:completed-proj"], check_in=False,
    )
    update_activity(result["activity_id"], status="completed")

    domains = get_active_domains()
    names = [d["name"] for d in domains]
    assert "completed-proj" not in names


def test_get_active_domains_excludes_non_domain(temp_db):
    """domain以外のnamespaceは返らない"""
    add_activity(
        title="Activity 1", description="Desc",
        tags=["intent:design"], check_in=False,
    )

    domains = get_active_domains()
    names = [d["name"] for d in domains]
    assert "design" not in names


def test_get_active_domains_sorted_by_name(temp_db):
    """name順ソート"""
    add_activity(title="Z", description="Desc", tags=["domain:zzz"], check_in=False)
    add_activity(title="A", description="Desc", tags=["domain:aaa"], check_in=False)

    domains = get_active_domains()
    names = [d["name"] for d in domains]
    aaa_idx = names.index("aaa")
    zzz_idx = names.index("zzz")
    assert aaa_idx < zzz_idx


def test_get_active_domains_deduplicates(temp_db):
    """同じdomain:タグを持つ複数アクティビティで重複しない"""
    add_activity(title="A1", description="Desc", tags=["domain:myproject"], check_in=False)
    add_activity(title="A2", description="Desc", tags=["domain:myproject"], check_in=False)

    domains = get_active_domains()
    myproject_domains = [d for d in domains if d["name"] == "myproject"]
    assert len(myproject_domains) == 1


def test_get_active_domains_no_activities(temp_db):
    """アクティビティがないdomainは返らない（トピックだけでは返らない）"""
    add_topic(title="Topic Only", description="Desc", tags=["domain:topic-only-proj"])

    domains = get_active_domains()
    names = [d["name"] for d in domains]
    assert "topic-only-proj" not in names


# ========================================
# get_active_activities_by_tag のテスト
# ========================================


def test_get_active_activities_by_tag_basic(temp_db):
    """domain:タグに紐づくホットアクティビティが返る"""
    add_activity(title="Activity 1", description="Desc", tags=["domain:test-proj"], check_in=False)

    tag_id = _get_tag_id("domain", "test-proj")
    activities = get_active_activities_by_tag(tag_id)

    assert len(activities) == 1
    assert activities[0]["title"] == "Activity 1"
    assert activities[0]["status"] == "pending"


def test_get_active_activities_by_tag_has_updated_at(temp_db):
    """updated_atフィールドが含まれる"""
    add_activity(title="Activity 1", description="Desc", tags=["domain:test-proj"], check_in=False)

    tag_id = _get_tag_id("domain", "test-proj")
    activities = get_active_activities_by_tag(tag_id)

    assert "updated_at" in activities[0]
    assert activities[0]["updated_at"] is not None


def test_get_active_activities_by_tag_excludes_completed(temp_db):
    """completedアクティビティは含まれない"""
    result = add_activity(title="Done Activity", description="Desc", tags=["domain:test-proj"], check_in=False)
    update_activity(result["activity_id"], status="completed")

    tag_id = _get_tag_id("domain", "test-proj")
    activities = get_active_activities_by_tag(tag_id)

    assert len(activities) == 0


def test_get_active_activities_by_tag_sort_order(temp_db):
    """in_progressが先、その後pending"""
    r1 = add_activity(title="Pending Activity", description="Desc", tags=["domain:test-proj"], check_in=False)
    r2 = add_activity(title="In Progress Activity", description="Desc", tags=["domain:test-proj"], check_in=False)
    update_activity(r2["activity_id"], status="in_progress")

    tag_id = _get_tag_id("domain", "test-proj")
    activities = get_active_activities_by_tag(tag_id)

    assert len(activities) == 2
    assert activities[0]["status"] == "in_progress"
    assert activities[1]["status"] == "pending"


def test_get_active_activities_by_tag_empty(temp_db):
    """紐づくアクティビティがなければ空リスト"""
    add_topic(title="Topic Only", description="Desc", tags=["domain:no-activities"])

    tag_id = _get_tag_id("domain", "no-activities")
    activities = get_active_activities_by_tag(tag_id)

    assert activities == []


# ========================================
# _build_activities_section のテスト
# ========================================


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
    """アクティビティがある場合、スコアリング対象セクションが生成される"""
    add_activity(title="[作業] 実装する", description="Desc", tags=["domain:myapp"], check_in=False)
    _age_all_activities()

    result = _build_active_context_wrapper()

    assert "# アクティビティ一覧" in result
    assert "## スコアリング対象" in result
    assert "[作業] 実装する" in result


def test_build_activities_section_status_marker_pending(temp_db):
    """pendingアクティビティに○マーカーが付く"""
    add_activity(title="[作業] 実装する", description="Desc", tags=["domain:myapp"], check_in=False)

    result = _build_active_context_wrapper()

    assert "○" in result


def test_build_activities_section_status_marker_in_progress(temp_db):
    """in_progressアクティビティに●マーカーが付く"""
    r = add_activity(title="[作業] 実装する", description="Desc", tags=["domain:myapp"], check_in=False)
    update_activity(r["activity_id"], status="in_progress")

    result = _build_active_context_wrapper()

    assert "●" in result


def test_build_activities_section_elapsed_days(temp_db):
    """経過日数がメタデータ行に 'updated: Nd ago' 形式で表示される"""
    add_activity(title="[作業] 実装する", description="Desc", tags=["domain:myapp"], check_in=False)

    result = _build_active_context_wrapper()

    assert "updated: 0d ago" in result


def test_build_activities_section_no_topic_section(temp_db):
    """トピックセクション（最新トピック:）が出力されない"""
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


def test_build_activities_section_all_items_shown(temp_db):
    """全アクティビティが番号付きフラットリストで表示される（件数制限なし）"""
    for i in range(7):
        r = add_activity(
            title=f"[作業] Activity {i}", description="Desc",
            tags=["domain:myapp"], check_in=False,
        )
        if i < 4:
            update_activity(r["activity_id"], status="in_progress")
    _age_all_activities()

    result = _build_active_context_wrapper()

    # 全7件が番号付きで表示される
    assert "1. ●" in result
    assert "全7件" in result
    # 全アクティビティが含まれている
    for i in range(7):
        assert f"Activity {i}" in result


def test_build_activities_section_numbered_list(temp_db):
    """アクティビティが連番で表示される"""
    add_activity(title="First", description="Desc", tags=["domain:myapp"], check_in=False)
    add_activity(title="Second", description="Desc", tags=["domain:myapp"], check_in=False)

    result = _build_active_context_wrapper()

    assert "1. " in result
    assert "2. " in result


def test_build_activities_section_total_count(temp_db):
    """全N件の合計が表示される"""
    for i in range(3):
        add_activity(
            title=f"[作業] Activity {i}", description="Desc",
            tags=["domain:myapp"], check_in=False,
        )
    _age_all_activities()

    result = _build_active_context_wrapper()

    assert "全3件" in result


def test_build_activities_section_domain_with_zero_activities_skipped(temp_db):
    """アクティビティ0件のdomainセクションはスキップ"""
    add_activity(title="Activity", description="Desc", tags=["domain:myapp"], check_in=False)

    # domain:emptyのタグだけ作成（アクティビティなし）
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


def test_build_activities_section_multiple_domains_flat(temp_db):
    """複数domainのアクティビティがフラットリストに統合される"""
    add_activity(title="App Activity", description="Desc", tags=["domain:app"], check_in=False)
    add_activity(title="Lib Activity", description="Desc", tags=["domain:lib"], check_in=False)
    _age_all_activities()

    result = _build_active_context_wrapper()

    assert "App Activity" in result
    assert "Lib Activity" in result
    # ドメインセクションではなくフラットリスト
    assert "## スコアリング対象" in result


def test_build_activities_section_activity_id_in_bracket(temp_db):
    """アクティビティIDが[id]形式で表示される"""
    activity = add_activity(title="Activity 1", description="Desc", tags=["domain:myapp"], check_in=False)
    activity_id = activity["activity_id"]

    result = _build_active_context_wrapper()

    assert f"[{activity_id}]" in result


def test_build_activities_section_raises_on_invalid_db(temp_db):
    """DB接続失敗時は例外が発生する（hookのmain()がcatchする前提）"""
    os.environ["DISCUSSION_DB_PATH"] = "/nonexistent/path/test.db"

    with pytest.raises(Exception):
        _build_active_context_wrapper()

    # 元に戻す
    os.environ["DISCUSSION_DB_PATH"] = temp_db


def test_build_activities_section_completed_activities_excluded(temp_db):
    """completedアクティビティは表示されない"""
    result = add_activity(title="Done Activity", description="Desc", tags=["domain:myapp"], check_in=False)
    update_activity(result["activity_id"], status="completed")

    ctx = _build_active_context_wrapper()

    assert "Done Activity" not in ctx


def test_build_activities_section_scoring_instructions(temp_db):
    """スコアリング指示が末尾に含まれる"""
    add_activity(title="[作業] Task", description="Desc", tags=["domain:myapp"], check_in=False)
    _age_all_activities()

    result = _build_active_context_wrapper()

    assert "# スコアリング指示" in result
    assert "上位5件を選び" in result
    assert "depends_on未完了" in result


def test_build_activities_section_tags_in_metadata(temp_db):
    """メタデータ行にタグ情報が含まれる"""
    add_activity(
        title="[作業] Task", description="Desc",
        tags=["domain:myapp", "intent:implement"], check_in=False,
    )

    result = _build_active_context_wrapper()

    assert "tags:" in result
    assert "domain:myapp" in result


def test_build_activities_section_description_snippet(temp_db):
    """メタデータ行にdescriptionスニペットが含まれる"""
    add_activity(
        title="[作業] Task", description="締め切りは来週金曜日",
        tags=["domain:myapp"], check_in=False,
    )

    result = _build_active_context_wrapper()

    assert "desc: 締め切りは来週金曜日" in result


def test_build_activities_section_description_snippet_truncated(temp_db):
    """descriptionが長い場合、先頭100文字に切り詰められる"""
    long_desc = "あ" * 200
    add_activity(
        title="[作業] Task", description=long_desc,
        tags=["domain:myapp"], check_in=False,
    )

    result = _build_active_context_wrapper()

    # 100文字に切り詰められている
    assert f"desc: {'あ' * 100}" in result
    assert "あ" * 101 not in result


def test_build_activities_section_blocked_by_metadata(temp_db):
    """未完了の依存先がblocked_byとしてメタデータに表示される"""
    r1 = add_activity(title="Dependency Task", description="Desc", tags=["domain:myapp"], check_in=False)
    r2 = add_activity(title="Blocked Task", description="Desc", tags=["domain:myapp"], check_in=False)

    # r2 depends_on r1
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

    assert result.count(f"[{aid}]") == 1


def test_build_activities_section_format(temp_db):
    """出力フォーマットが仕様通り: ヘッダ + スコアリング対象 + 番号付き行 + メタデータ行"""
    r = add_activity(
        title="[議論] stop_hookのスキップ機能", description="機能の設計",
        tags=["domain:cc-memory"], check_in=False,
    )
    update_activity(r["activity_id"], status="in_progress")
    add_activity(
        title="[作業] アクティブコンテキスト改善", description="改善作業",
        tags=["domain:cc-memory"], check_in=False,
    )
    _age_all_activities()

    result = _build_active_context_wrapper()

    lines = result.strip().split("\n")
    assert lines[0] == "# アクティビティ一覧"
    assert lines[1] == ""
    assert lines[2] == "## スコアリング対象"
    # in_progressが先に来る（番号付き）
    assert lines[3].startswith("1. ●")
    assert "[議論] stop_hookのスキップ機能" in lines[3]
    # メタデータ行
    assert "updated:" in lines[4]
    # pendingが後に来る
    assert lines[5].startswith("2. ○")
    assert "[作業] アクティブコンテキスト改善" in lines[5]
