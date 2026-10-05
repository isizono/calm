"""scripts/ops_metrics.py の単体テスト

signal_events テーブルに record_signal で fixture 行を積み、定義式どおりの
巻き戻し率・shadow乖離率・矛盾/miss/誤類推件数・goal観測が計算されること、
分母0での N/A 表示、--packages-file 供給/未供給でのフォールバックを検証する。
search_telemetry/precedent_telemetry/fetch_telemetry/citation_event_log の
集計、guard_block件数、書き手コードが無いkindのno_writer_codeフラグも対象。
"""
import json
import sqlite3

import pytest
from test_migrations.conftest import db_before_migration

from scripts.ops_metrics import (
    compute_metrics,
    format_text,
    load_packages,
    main,
)
from src.services import goal_service as gs
from src.services import signal_service as ss
from src.services.activity_service import add_activity, update_activity


def _backdate(db_path: str, signal_id: int, days_ago: int) -> None:
    """指定シグナルの first_seen_at / last_seen_at を days_ago 日前に書き換える。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE signal_events
            SET first_seen_at = datetime('now', ?), last_seen_at = datetime('now', ?)
            WHERE id = ?
            """,
            (f"-{days_ago} days", f"-{days_ago} days", signal_id),
        )
        conn.commit()
    finally:
        conn.close()


def _backdate_goal_judged_at(db_path: str, goal_id: int, days_ago: int) -> None:
    """指定goalのjudged_atをdays_ago日前に書き換える。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE goals SET judged_at = datetime('now', ?) WHERE id = ?",
            (f"-{days_ago} days", goal_id),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_search_telemetry(
    db_path: str,
    *,
    diagnostics: dict | None = None,
    results: list | None = None,
    caller_session_id: str | None = None,
    timestamp: str | None = None,
) -> int:
    """search_telemetry に1行挿入する(search()実行時に書かれる実際の列形状を再現)。"""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO search_telemetry
                (query, parameters, result_count, results_json, diagnostics_json,
                 caller_session_id, timestamp)
            VALUES (?, '{}', 0, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (
                "q",
                json.dumps(results) if results is not None else None,
                json.dumps(diagnostics) if diagnostics is not None else None,
                caller_session_id,
                timestamp,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _insert_fetch_telemetry(
    db_path: str,
    *,
    items: list,
    caller_session_id: str | None = None,
    timestamp: str | None = None,
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO fetch_telemetry (tool, items_json, caller_session_id, timestamp)
            VALUES ('get_by_ids', ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (json.dumps(items), caller_session_id, timestamp),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _insert_precedent_telemetry(
    db_path: str, *, guarantee: str, decisions_total: int = 0, full_count: int = 0
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO precedent_telemetry
                (context, parameters, guarantee, routing_json, decisions_total, full_count)
            VALUES ('ctx', '{}', ?, '{}', ?, ?)
            """,
            (guarantee, decisions_total, full_count),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _backdate_telemetry(db_path: str, table: str, row_id: int, days_ago: int, ts_col: str = "timestamp") -> None:
    """telemetryテーブル1行のtimestamp系カラムをdays_ago日前に書き換える(window_days検証用)。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE {table} SET {ts_col} = datetime('now', ?) WHERE id = ?",
            (f"-{days_ago} days", row_id),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_citation_event_log(db_path: str, *, verification_result: str | None) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO citation_event_log
                (source, before_text, after_text, verification_result)
            VALUES ('write_auto_convert', 'before', 'after', ?)
            """,
            (verification_result,),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


class TestContradictionMetrics:
    def test_counts_by_resolution(self, temp_db):
        ss.record_signal("contradiction", "a vs b", source="agent",
                          context={"resolution": "existing_correct"})
        ss.record_signal("contradiction", "c vs d", source="agent",
                          context={"resolution": "new_correct"})
        ss.record_signal("contradiction", "e vs f", source="agent",
                          context={"resolution": "unresolved"})
        ss.record_signal("contradiction", "g vs h", source="agent", context=None)

        metrics = compute_metrics(temp_db, window_days=None)

        c = metrics["contradiction"]
        assert c["count"] == 4
        assert c["by_resolution"] == {
            "existing_correct": 1,
            "new_correct": 1,
            "unresolved": 1,
            "unknown": 1,
        }


class TestRollbackMetrics:
    def test_rate_computed_from_definition(self, temp_db):
        # post_veto_candidate(live) の分母イベント2件、うちrollback1件
        ss.record_signal(
            "boundary_case", "case PR#1", source="gate",
            context={"mode": "live", "machine_verdict": "post_veto_candidate"},
        )
        ss.record_signal(
            "boundary_case", "case PR#2", source="gate",
            context={"mode": "live", "machine_verdict": "post_veto_candidate"},
        )
        # 対象外(mode=shadow)は分母に入らない
        ss.record_signal(
            "boundary_case", "case PR#3", source="gate",
            context={"mode": "shadow", "machine_verdict": "post_veto_candidate"},
        )
        # 対象外(machine_verdict=pre_go)は分母に入らない
        ss.record_signal(
            "boundary_case", "case PR#4", source="gate",
            context={"mode": "live", "machine_verdict": "pre_go"},
        )
        ss.record_signal("rollback", "revert PR#1", source="gate",
                          context={"target": {"pr": 1}, "reason": "bug"})

        metrics = compute_metrics(temp_db, window_days=None)

        r = metrics["rollback"]
        assert r["rollback_count"] == 1
        assert r["post_veto_live_count"] == 2
        assert r["rate"] == pytest.approx(0.5)

    def test_rate_is_none_when_denominator_zero(self, temp_db):
        metrics = compute_metrics(temp_db, window_days=None)

        r = metrics["rollback"]
        assert r["rollback_count"] == 0
        assert r["post_veto_live_count"] == 0
        assert r["rate"] is None
        assert "N/A" in format_text(metrics)


class TestShadowDivergenceMetrics:
    def test_divergence_and_false_negative_rate(self, temp_db):
        ss.record_signal("boundary_case", "shadow case 1", source="gate",
                          context={"mode": "shadow", "divergence": "none"})
        ss.record_signal("boundary_case", "shadow case 2", source="gate",
                          context={"mode": "shadow", "divergence": "false_negative"})
        ss.record_signal("boundary_case", "shadow case 3", source="gate",
                          context={"mode": "shadow", "divergence": "false_positive"})
        ss.record_signal("boundary_case", "shadow case 4", source="gate",
                          context={"mode": "shadow", "divergence": "gray_case"})
        # live は shadow の分母に入らない
        ss.record_signal("boundary_case", "live case", source="gate",
                          context={"mode": "live", "machine_verdict": "pre_go"})

        metrics = compute_metrics(temp_db, window_days=None)

        s = metrics["shadow_divergence"]
        assert s["shadow_total"] == 4
        assert s["diverged_count"] == 3
        assert s["divergence_rate"] == pytest.approx(0.75)
        assert s["false_negative_count"] == 1
        assert s["false_negative_rate"] == pytest.approx(0.25)

    def test_rate_is_none_when_no_shadow_cases(self, temp_db):
        metrics = compute_metrics(temp_db, window_days=None)

        s = metrics["shadow_divergence"]
        assert s["shadow_total"] == 0
        assert s["divergence_rate"] is None
        assert s["false_negative_rate"] is None


class TestPullAndMisappliedMetrics:
    _PACKAGES = [
        {
            "precedents": [
                {"ref": "decision 1", "stance": "applied"},
                {"ref": "decision 2", "stance": "out_of_scope"},
            ],
            "pull": {"presented": [{"type": "decision", "id": 1}, {"type": "decision", "id": 3}]},
        },
        {
            "precedents": [{"ref": "decision 4", "stance": "applied"}],
            "pull": {"presented": "unavailable"},
        },
    ]

    def test_without_packages_file_returns_count_only(self, temp_db):
        ss.record_signal("precedent_miss", "missed decision 9", source="agent",
                          context={"missed_ids": [{"type": "decision", "id": 9}]})
        ss.record_signal("precedent_misapplied", "decision 1 out of scope", source="agent",
                          context={"cited_id": {"type": "decision", "id": 1}})

        metrics = compute_metrics(temp_db, window_days=None, packages=None)

        assert metrics["pull"] == {"miss_count": 1, "no_writer_code": True}
        assert metrics["precedent_misapplied"] == {"misapplied_count": 1, "no_writer_code": True}
        text = format_text(metrics)
        assert "率は算出不可" in text
        assert "書き手コードなし" in text

    def test_with_packages_file_computes_rates(self, temp_db):
        ss.record_signal("precedent_miss", "missed decision 9", source="agent")
        ss.record_signal("precedent_misapplied", "decision 1 out of scope", source="agent")

        metrics = compute_metrics(temp_db, window_days=None, packages=self._PACKAGES)

        # citation_slot_count = precedents(2+1) + presented(2, unavailableは非対象) = 5
        assert metrics["pull"]["citation_slot_count"] == 5
        assert metrics["pull"]["miss_count"] == 1
        assert metrics["pull"]["miss_rate"] == pytest.approx(1 / 5)

        # applied_citation_count = stance=applied の2件
        assert metrics["precedent_misapplied"]["applied_citation_count"] == 2
        assert metrics["precedent_misapplied"]["misapplied_count"] == 1
        assert metrics["precedent_misapplied"]["misapplied_rate"] == pytest.approx(0.5)


class TestWindowDaysFiltering:
    def test_rows_outside_window_are_excluded(self, temp_db):
        ss.record_signal("rollback", "revert PR#recent", source="gate")
        old = ss.record_signal("rollback", "revert PR#old", source="gate")
        _backdate(temp_db, old["id"], days_ago=90)

        metrics = compute_metrics(temp_db, window_days=30)

        assert metrics["rollback"]["rollback_count"] == 1

        metrics_all = compute_metrics(temp_db, window_days=None)
        assert metrics_all["rollback"]["rollback_count"] == 2


class TestGoalMetrics:
    def _closed_goal(self, handle: str) -> tuple[int, int]:
        """条件1件が既に満たされた状態のgoalを新規作成し、即座にachievedで閉じる。"""
        act = add_activity(title=f"act-{handle}", description="d", tags=["domain:test"], check_in=False)[
            "activity_id"
        ]
        goal_id = gs.set_goal(
            act,
            {
                "new": {
                    "handle": handle,
                    "statement": "終わりの一文",
                    "conditions": [{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}],
                }
            },
        )["goal_id_raw"]
        gs.judge_goal(goal_id, "achieved", note="達成した")
        return goal_id, act

    def test_rollback_count_excludes_kind_rollback_and_vice_versa(self, temp_db):
        # 既存の巻き戻し(kind='rollback')を2件積んでおく。件数を非対称にして、
        # goalの差し戻し(1件)と取り違えていないかを区別できるようにする。
        ss.record_signal("rollback", "revert PR#9", source="gate")
        ss.record_signal("rollback", "revert PR#10", source="gate")
        goal_id, _ = self._closed_goal("goal-x")
        gs.update_goal(goal_id, reopen_reason="判定が誤りだった")

        metrics = compute_metrics(temp_db, window_days=None)

        assert metrics["goal"]["rollback_count"] == 1
        assert metrics["rollback"]["rollback_count"] == 2

    def test_judged_count_is_closed_goals_plus_rollback_rows(self, temp_db):
        self._closed_goal("goal-a")
        goal_b, _ = self._closed_goal("goal-b")
        self._closed_goal("goal-c")
        gs.update_goal(goal_b, reopen_reason="判定が誤りだった")

        metrics = compute_metrics(temp_db, window_days=None)

        g = metrics["goal"]
        assert g["rollback_count"] == 1
        assert g["judged_count"] == 3  # 閉じているgoal(a, c)=2 + 差し戻し行数=1
        assert g["misjudgment_rate"] == pytest.approx(1 / 3)

    def test_judged_count_denominator_scoped_to_same_window_as_rollback(self, temp_db):
        """呼び出し元が使うデフォルト相当の期間(30日)で、分母(judged_count)も
        分子(rollback_count)と同じ期間に絞られることを確認する。window外の
        判定済みgoalが分母に混入すると誤判定率が実態より低く出る"""
        self._closed_goal("goal-recent")
        old_goal_id, _ = self._closed_goal("goal-old")
        _backdate_goal_judged_at(temp_db, old_goal_id, days_ago=90)

        metrics = compute_metrics(temp_db, window_days=30)
        assert metrics["goal"]["judged_count"] == 1

        metrics_all = compute_metrics(temp_db, window_days=None)
        assert metrics_all["goal"]["judged_count"] == 2

    def test_misjudgment_rate_is_none_when_nothing_judged_yet(self, temp_db):
        metrics = compute_metrics(temp_db, window_days=None)

        g = metrics["goal"]
        assert g["judged_count"] == 0
        assert g["misjudgment_rate"] is None

    def test_neglected_counts_goal_completed_outside_judge_goal(self, temp_db):
        act = add_activity(title="act-neglect", description="d", tags=["domain:test"], check_in=False)[
            "activity_id"
        ]
        gs.set_goal(
            act,
            {"new": {"handle": "goal-neglect", "statement": "終わりの一文", "conditions": [{"statement": "c1", "actor": "claude"}]}},
        )
        # judge_goalを経ずに直接completedにする(update_activityは未判定goalを拒否しない設計)
        update_activity(act, status="completed", closed_by="claude")

        metrics = compute_metrics(temp_db, window_days=None)

        assert metrics["goal"]["neglected_count"] == 1

    def test_neglected_excludes_goal_with_an_incomplete_linked_activity(self, temp_db):
        act1 = add_activity(title="act-a", description="d", tags=["domain:test"], check_in=False)["activity_id"]
        act2 = add_activity(title="act-b", description="d", tags=["domain:test"], check_in=False)["activity_id"]
        goal_id = gs.set_goal(
            act1,
            {"new": {"handle": "goal-mixed", "statement": "終わりの一文", "conditions": [{"statement": "c1", "actor": "claude"}]}},
        )["goal_id_raw"]
        gs.set_goal(act2, {"goal_id": goal_id})
        update_activity(act1, status="completed", closed_by="claude")
        # act2は未完了のまま残す

        metrics = compute_metrics(temp_db, window_days=None)

        assert metrics["goal"]["neglected_count"] == 0

    def test_no_crash_when_goal_tables_absent(self):
        with db_before_migration("0077_add_goals") as db_path:
            ss.record_signal("contradiction", "a vs b", source="agent", context={"resolution": "unresolved"})

            metrics = compute_metrics(db_path, window_days=None)

            assert metrics["goal"] is None
            assert metrics["contradiction"]["count"] == 1  # 他の指標は普段どおり動く
            text = format_text(metrics)
            assert "goal" not in text


class TestSearchTelemetryMetrics:
    def test_degraded_and_qe_rates_computed_from_diagnostics(self, temp_db):
        _insert_search_telemetry(temp_db, diagnostics={"degraded": True, "qe_expansions": []})
        _insert_search_telemetry(temp_db, diagnostics={"degraded": True, "qe_expansions": ["x"]})
        _insert_search_telemetry(temp_db, diagnostics={"degraded": False, "qe_expansions": ["x"]})
        _insert_search_telemetry(temp_db, diagnostics={"degraded": False, "qe_expansions": []})

        metrics = compute_metrics(temp_db, window_days=None)

        st = metrics["search_telemetry"]
        assert st["total_count"] == 4
        assert st["diagnostics_count"] == 4
        assert st["degraded_count"] == 2
        assert st["degraded_rate"] == pytest.approx(0.5)
        assert st["qe_fired_count"] == 2
        assert st["qe_fire_rate"] == pytest.approx(0.5)

    def test_rows_without_diagnostics_excluded_from_denominator(self, temp_db):
        _insert_search_telemetry(temp_db, diagnostics=None)  # migration 0054以前相当の旧行
        _insert_search_telemetry(temp_db, diagnostics={"degraded": True, "qe_expansions": []})

        metrics = compute_metrics(temp_db, window_days=None)

        st = metrics["search_telemetry"]
        assert st["total_count"] == 2
        assert st["diagnostics_count"] == 1
        assert st["degraded_count"] == 1
        assert st["degraded_rate"] == pytest.approx(1.0)

    def test_empty_table_returns_none_rates(self, temp_db):
        metrics = compute_metrics(temp_db, window_days=None)

        st = metrics["search_telemetry"]
        assert st == {
            "total_count": 0,
            "diagnostics_count": 0,
            "degraded_count": 0,
            "degraded_rate": None,
            "qe_fired_count": 0,
            "qe_fire_rate": None,
        }


class TestPrecedentTelemetryMetrics:
    def test_guarantee_breakdown_and_coverage(self, temp_db):
        _insert_precedent_telemetry(temp_db, guarantee="enumerated", decisions_total=4, full_count=4)
        _insert_precedent_telemetry(temp_db, guarantee="enumerated", decisions_total=4, full_count=2)
        _insert_precedent_telemetry(temp_db, guarantee="routing_miss")
        _insert_precedent_telemetry(temp_db, guarantee="routing_unavailable")

        metrics = compute_metrics(temp_db, window_days=None)

        pt = metrics["precedent_telemetry"]
        assert pt["count"] == 4
        assert pt["by_guarantee"] == {
            "enumerated": 2, "routing_miss": 1, "routing_unavailable": 1, "unknown": 0,
        }
        # (4/4 + 2/4) / 2 = 0.75
        assert pt["enumerated_full_coverage_rate"] == pytest.approx(0.75)

    def test_enumerated_with_zero_decisions_excluded_from_coverage(self, temp_db):
        """decisions_total=0のenumerated行(対象decisionが0件)はカバレッジ平均の対象外。"""
        _insert_precedent_telemetry(temp_db, guarantee="enumerated", decisions_total=0, full_count=0)

        metrics = compute_metrics(temp_db, window_days=None)

        assert metrics["precedent_telemetry"]["enumerated_full_coverage_rate"] is None

    def test_empty_table_returns_zero_counts(self, temp_db):
        metrics = compute_metrics(temp_db, window_days=None)

        pt = metrics["precedent_telemetry"]
        assert pt["count"] == 0
        assert pt["enumerated_full_coverage_rate"] is None


class TestFetchFollowMetrics:
    def test_follow_rate_when_fetch_occurs_after_search(self, temp_db):
        _insert_search_telemetry(
            temp_db,
            results=[{"type": "decision", "id": 1}],
            caller_session_id="s1",
            timestamp="2026-01-01 00:00:00",
        )
        _insert_fetch_telemetry(
            temp_db,
            items=[{"type": "decision", "id": 1}],
            caller_session_id="s1",
            timestamp="2026-01-01 00:00:05",
        )

        metrics = compute_metrics(temp_db, window_days=None)

        ff = metrics["fetch_follow"]
        assert ff["search_result_count"] == 1
        assert ff["followed_count"] == 1
        assert ff["follow_rate"] == pytest.approx(1.0)

    def test_fetch_before_search_not_counted(self, temp_db):
        """fetchがsearchより前に起きた場合は「後続」ではないため追随に数えない。"""
        _insert_fetch_telemetry(
            temp_db,
            items=[{"type": "decision", "id": 1}],
            caller_session_id="s1",
            timestamp="2026-01-01 00:00:00",
        )
        _insert_search_telemetry(
            temp_db,
            results=[{"type": "decision", "id": 1}],
            caller_session_id="s1",
            timestamp="2026-01-01 00:00:05",
        )

        metrics = compute_metrics(temp_db, window_days=None)

        ff = metrics["fetch_follow"]
        assert ff["search_result_count"] == 1
        assert ff["followed_count"] == 0

    def test_followed_when_item_also_fetched_before_search(self, temp_db):
        """同一itemが検索より前に一度fetchされていても、検索後の再fetchがあれば追随に数える。

        最初のfetch時刻だけを見ると「検索前のfetch」で後続判定が止まってしまうため、
        同一(type,id)の最後のfetch時刻を使う必要がある。
        """
        _insert_fetch_telemetry(
            temp_db,
            items=[{"type": "decision", "id": 1}],
            caller_session_id="s1",
            timestamp="2026-01-01 00:00:00",
        )
        _insert_search_telemetry(
            temp_db,
            results=[{"type": "decision", "id": 1}],
            caller_session_id="s1",
            timestamp="2026-01-01 00:00:05",
        )
        _insert_fetch_telemetry(
            temp_db,
            items=[{"type": "decision", "id": 1}],
            caller_session_id="s1",
            timestamp="2026-01-01 00:00:10",
        )

        metrics = compute_metrics(temp_db, window_days=None)

        assert metrics["fetch_follow"]["followed_count"] == 1

    def test_different_session_not_counted(self, temp_db):
        _insert_search_telemetry(
            temp_db, results=[{"type": "decision", "id": 1}], caller_session_id="s1",
        )
        _insert_fetch_telemetry(
            temp_db, items=[{"type": "decision", "id": 1}], caller_session_id="s2",
        )

        metrics = compute_metrics(temp_db, window_days=None)

        assert metrics["fetch_follow"]["followed_count"] == 0

    def test_empty_tables_return_none_rate(self, temp_db):
        metrics = compute_metrics(temp_db, window_days=None)

        ff = metrics["fetch_follow"]
        assert ff == {"search_result_count": 0, "followed_count": 0, "follow_rate": None}


class TestCitationEventLogMetrics:
    def test_verification_result_breakdown(self, temp_db):
        _insert_citation_event_log(temp_db, verification_result="exists")
        _insert_citation_event_log(temp_db, verification_result="exists")
        _insert_citation_event_log(temp_db, verification_result="dangling")
        _insert_citation_event_log(temp_db, verification_result="skip")
        _insert_citation_event_log(temp_db, verification_result=None)

        metrics = compute_metrics(temp_db, window_days=None)

        ce = metrics["citation_event_log"]
        assert ce["count"] == 5
        assert ce["by_verification_result"] == {
            "exists": 2, "dangling": 1, "skip": 1, "not_verified": 1,
        }

    def test_empty_table_returns_zero(self, temp_db):
        metrics = compute_metrics(temp_db, window_days=None)

        ce = metrics["citation_event_log"]
        assert ce["count"] == 0
        assert ce["by_verification_result"] == {
            "exists": 0, "dangling": 0, "skip": 0, "not_verified": 0,
        }


class TestGuardBlockMetrics:
    def test_counts_by_rule_using_occurrence_sum(self, temp_db):
        """同一(source, summary)の再発はsignal_events側で1行に畳まれる
        (occurrence_count加算)ため、SUM(occurrence_count)で件数を数える。"""
        ss.record_signal("guard_block", "internal ID literal blocked (code)", source="hook:preblock")
        ss.record_signal("guard_block", "internal ID literal blocked (code)", source="hook:preblock")
        ss.record_signal("guard_block", "nested bg spawn blocked", source="hook:deny_nested_bg")

        metrics = compute_metrics(temp_db, window_days=None)

        gb = metrics["guard_block"]
        assert gb["total_count"] == 3
        by_rule = {(r["source"], r["summary"]): r["count"] for r in gb["by_rule"]}
        assert by_rule[("hook:preblock", "internal ID literal blocked (code)")] == 2
        assert by_rule[("hook:deny_nested_bg", "nested bg spawn blocked")] == 1

    def test_window_days_filters_by_last_seen_at(self, temp_db):
        ss.record_signal("guard_block", "nested bg spawn blocked", source="hook:deny_nested_bg")
        old = ss.record_signal("guard_block", "internal ID literal blocked (code)", source="hook:preblock")
        _backdate(temp_db, old["id"], days_ago=90)

        metrics = compute_metrics(temp_db, window_days=30)
        assert metrics["guard_block"]["total_count"] == 1

        metrics_all = compute_metrics(temp_db, window_days=None)
        assert metrics_all["guard_block"]["total_count"] == 2

    def test_empty_returns_zero(self, temp_db):
        metrics = compute_metrics(temp_db, window_days=None)
        assert metrics["guard_block"] == {"total_count": 0, "by_rule": []}


class TestNoWriterCodeFlag:
    """precedent_miss/precedent_misapplied/boundary_case/rollbackは書き手の自動化
    コードが存在しないkindであることをno_writer_codeフラグで明示する(構造的0件の可視化)。"""

    def test_flag_present_even_when_count_is_nonzero(self, temp_db):
        """手動report_signalでboundary_caseが記録されても(0件ではなくなっても)、
        フラグは「書き手コードが無い」という構造的事実を表すため変わらず立つ。"""
        ss.record_signal(
            "boundary_case", "case PR#1", source="gate",
            context={"mode": "live", "machine_verdict": "post_veto_candidate"},
        )

        metrics = compute_metrics(temp_db, window_days=None)

        assert metrics["rollback"]["no_writer_code"] is True
        assert metrics["shadow_divergence"]["no_writer_code"] is True

    def test_flag_absent_from_goal_metrics(self, temp_db):
        """goal_rollbackは書き手コード(goal_service)が実在するため対象外。"""
        metrics = compute_metrics(temp_db, window_days=None)
        assert "no_writer_code" not in (metrics["goal"] or {})


class TestComputeMetricsEmptyDb:
    def test_all_new_sections_present_and_do_not_crash(self, temp_db):
        """全telemetryテーブル・signal_eventsが空でも例外にならず、各節が現れる。"""
        metrics = compute_metrics(temp_db, window_days=None)

        for key in (
            "search_telemetry", "precedent_telemetry", "fetch_follow",
            "citation_event_log", "guard_block",
        ):
            assert key in metrics

        text = format_text(metrics)
        assert "guard_block件数: 0 件" in text


class TestLoadPackages:
    def test_load_packages_reads_json_array(self, tmp_path):
        packages_file = tmp_path / "packages.json"
        packages_file.write_text(json.dumps(self.__class__._sample()), encoding="utf-8")

        loaded = load_packages(str(packages_file))

        assert loaded == self.__class__._sample()

    def test_load_packages_none_when_not_given(self):
        assert load_packages(None) is None

    def test_load_packages_rejects_non_array(self, tmp_path):
        packages_file = tmp_path / "packages.json"
        packages_file.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

        with pytest.raises(ValueError):
            load_packages(str(packages_file))

    def test_load_packages_rejects_non_dict_element(self, tmp_path):
        packages_file = tmp_path / "packages.json"
        packages_file.write_text(json.dumps([{"precedents": []}, 42]), encoding="utf-8")

        with pytest.raises(ValueError):
            load_packages(str(packages_file))

    @staticmethod
    def _sample():
        return [{"precedents": [], "pull": {"presented": "unavailable"}}]


class TestMainCli:
    def test_main_json_output(self, temp_db, capsys):
        ss.record_signal("contradiction", "a vs b", source="agent",
                          context={"resolution": "unresolved"})

        exit_code = main(["--db", temp_db, "--json", "--window-days", "0"])

        assert exit_code == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["window_days"] is None
        assert payload["contradiction"]["count"] == 1

    def test_main_text_output(self, temp_db, capsys):
        exit_code = main(["--db", temp_db])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "ops_metrics" in out
        assert "矛盾イベント数" in out

    def test_main_with_packages_file(self, temp_db, tmp_path, capsys):
        packages_file = tmp_path / "packages.json"
        packages_file.write_text(
            json.dumps([{"precedents": [{"ref": "decision 1", "stance": "applied"}],
                         "pull": {"presented": []}}]),
            encoding="utf-8",
        )
        ss.record_signal("precedent_misapplied", "decision 1 out of scope", source="agent")

        exit_code = main(["--db", temp_db, "--json", "--packages-file", str(packages_file)])

        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["precedent_misapplied"]["misapplied_rate"] == pytest.approx(1.0)

    def test_main_rejects_broken_packages_file(self, temp_db, tmp_path, capsys):
        packages_file = tmp_path / "broken.json"
        packages_file.write_text("not json", encoding="utf-8")

        exit_code = main(["--db", temp_db, "--packages-file", str(packages_file)])

        assert exit_code == 1
        assert "読み込みに失敗" in capsys.readouterr().err

    def test_main_rejects_non_dict_package_element(self, temp_db, tmp_path, capsys):
        """トップレベル要素が JSON object でない --packages-file を load 時に弾く。"""
        packages_file = tmp_path / "bad.json"
        packages_file.write_text(json.dumps(["not an object"]), encoding="utf-8")

        exit_code = main(["--db", temp_db, "--packages-file", str(packages_file)])

        assert exit_code == 1
        assert "読み込みに失敗" in capsys.readouterr().err

    def test_main_converts_malformed_precedents_to_controlled_error(self, temp_db, tmp_path, capsys):
        """precedents に dict でない要素が混じる --packages-file を素の traceback にせず終了コード1へ変換する。"""
        packages_file = tmp_path / "bad.json"
        packages_file.write_text(
            json.dumps([{"precedents": ["not a dict"], "pull": {"presented": []}}]),
            encoding="utf-8",
        )

        exit_code = main(["--db", temp_db, "--packages-file", str(packages_file)])

        assert exit_code == 1
        assert "データ形式が不正" in capsys.readouterr().err
