"""hooks/session_start_hook.py の予算宣言セクション構成に対する回帰テスト

1. CIゼロサムテスト: 宣言予算の合計がTOTAL_INJECTION_BUDGET_CHARSを超えないこと
   （超えると個々のセクションがbudget_chars以内に収まっていてもcompose()全体の
   予算保証が成立しなくなる）
2. 膨張fixtureテスト: 既存の各セクション実装内部に存在する無制限箇所
   （heartbeat中の他セッションactivity全件列挙・signalsのkind内訳無制限・
   SYNC_POLICYの自由長環境変数）を実際にデータで膨張させ、compose()経由の
   additionalContext合計がTOTAL_INJECTION_BUDGET_CHARS以内に収まることを検証する。
   個々のセクション内部実装がこの膨張を自前で防いでいなくても、compose()の
   ハード切り詰めにより全体予算は常に守られるという契約の確認が目的。
"""
from datetime import datetime, timezone

from hooks import session_start_hook
from src import config
from src.db import get_connection
from src.services.injection_compositor import total_declared_budget


def test_declared_budgets_do_not_exceed_total():
    """Σ budget_chars <= TOTAL_INJECTION_BUDGET_CHARSであること。

    セクションを追加・予算調整する際は必ずこのテストと整合させること
    （超えると本テストが落ちて気付ける設計）。
    """
    assert total_declared_budget(session_start_hook._SECTIONS) <= config.TOTAL_INJECTION_BUDGET_CHARS


def _seed_domain_tag(conn, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM tags WHERE namespace = 'domain' AND name = ?", (name,)
    ).fetchone()
    if row:
        return row["id"]
    cursor = conn.execute("INSERT INTO tags (namespace, name) VALUES ('domain', ?)", (name,))
    return cursor.lastrowid


def _seed_many_heartbeat_activities(conn, count: int, domain_tag_id: int) -> None:
    """別セッションでheartbeat中のactivityをcount件作成する（階層1は件数上限が無い）。"""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for i in range(count):
        cursor = conn.execute(
            "INSERT INTO activities "
            "(title, description, status, last_heartbeat_at, last_heartbeat_session_id) "
            "VALUES (?, ?, 'in_progress', ?, ?)",
            (f"[作業] heartbeat膨張タスク{i}", "desc", now_iso, f"sess-other-{i}"),
        )
        activity_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO activity_tags (activity_id, tag_id) VALUES (?, ?)",
            (activity_id, domain_tag_id),
        )


def _seed_many_signal_kinds(conn, count: int) -> None:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for i in range(count):
        conn.execute(
            "INSERT INTO signal_events "
            "(kind, source, summary, fingerprint, first_seen_at, last_seen_at, status) "
            "VALUES (?, 'tool:test', ?, ?, ?, ?, 'new')",
            (f"kind_{i}", f"summary {i}", f"fp-{i}", now_iso, now_iso),
        )


class TestInflationFixturesStayWithinBudget:
    """既存セクション実装内部の無制限箇所を実際に膨張させても、compose()経由の
    additionalContext合計がTOTAL_INJECTION_BUDGET_CHARS以内に収まることを検証する"""

    def test_many_heartbeat_active_activities_stays_within_budget(self, temp_db):
        conn = get_connection()
        try:
            domain_tag_id = _seed_domain_tag(conn, "inflation-test")
            _seed_many_heartbeat_activities(conn, 1000, domain_tag_id)
            conn.commit()
        finally:
            conn.close()

        result = session_start_hook._build_session_context(session_id="sess-self")

        assert len(result) <= config.TOTAL_INJECTION_BUDGET_CHARS, (
            f"1000件のheartbeat activityでadditionalContextが予算超過（実測{len(result)}字）"
        )

    def test_many_signal_kinds_stays_within_budget(self, temp_db):
        conn = get_connection()
        try:
            _seed_many_signal_kinds(conn, 1000)
            conn.commit()
        finally:
            conn.close()

        result = session_start_hook._build_session_context()

        assert len(result) <= config.TOTAL_INJECTION_BUDGET_CHARS, (
            f"1000種のsignal kindでadditionalContextが予算超過（実測{len(result)}字）"
        )

    def test_huge_sync_policy_stays_within_budget(self, temp_db, monkeypatch):
        monkeypatch.setattr(config, "SYNC_POLICY", "x" * 50000)

        result = session_start_hook._build_session_context()

        assert len(result) <= config.TOTAL_INJECTION_BUDGET_CHARS, (
            f"50,000字のSYNC_POLICYでadditionalContextが予算超過（実測{len(result)}字）"
        )
