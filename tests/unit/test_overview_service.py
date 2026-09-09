"""overview_service の単体テスト。

4節（working / recently_done / awaiting_human / backlog）の分類境界、
COALESCE/MAX()の落とし穴回帰検出、orch_managedを除外しないこと、副作用ゼロ、
引数バリデーション、limitの丸めと切り詰めを検証する。
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.config import HEARTBEAT_TIMEOUT_MINUTES, SNOOZE_DURATION_DAYS
from src.db import get_connection
from src.services import ask_service
from src.services import overview_service as ov
from src.services.activity_service import add_activity, update_activity
from src.services.tag_service import update_tag


@pytest.fixture(autouse=True)
def _disable_embedding(monkeypatch):
    """embeddingサーバーを無効化する(test_active_context.pyと同じパターン)。"""
    import src.services.embedding_service as emb
    monkeypatch.setattr(emb, "_server_initialized", False)
    monkeypatch.setattr(emb, "_backfill_done", True)
    monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)


def _make_activity(
    title: str = "a",
    status: str = "pending",
    tags: list[str] | None = None,
    orch_managed: bool = False,
) -> int:
    activity_id = add_activity(
        title=title, description="d", tags=tags or ["domain:test"],
        check_in=False, orch_managed=orch_managed,
    )["activity_id"]
    if status != "pending":
        update_activity(activity_id, status=status)
    return activity_id


def _utc_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _minutes_ago(minutes: int) -> str:
    return _utc_str(datetime.now(timezone.utc) - timedelta(minutes=minutes))


def _days_ago(days: int) -> str:
    return _utc_str(datetime.now(timezone.utc) - timedelta(days=days))


def _set_updated_at(activity_id: int, value: str) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE activities SET updated_at = ? WHERE id = ?", (value, activity_id))
        conn.commit()
    finally:
        conn.close()


def _set_heartbeat(activity_id: int, value: str) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE activities SET last_heartbeat_at = ? WHERE id = ?", (value, activity_id))
        conn.commit()
    finally:
        conn.close()


def _get_status(activity_id: int) -> str:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT status FROM activities WHERE id = ?", (activity_id,)
        ).fetchone()["status"]
    finally:
        conn.close()


class TestValidation:
    def test_days_zero_is_rejected(self, temp_db):
        result = ov.get_overview(days=0)
        assert result["error"]["code"] == "INVALID_PARAMETER"

    def test_limit_zero_is_rejected(self, temp_db):
        result = ov.get_overview(limit=0)
        assert result["error"]["code"] == "INVALID_PARAMETER"

    def test_negative_days_is_rejected(self, temp_db):
        result = ov.get_overview(days=-1)
        assert result["error"]["code"] == "INVALID_PARAMETER"


class TestWorkingSection:
    def test_null_heartbeat_in_progress_splits_by_freshness_and_neither_row_disappears(self, temp_db):
        """COALESCEを外すとfresh/stale双方の行が両節から消えるregressionの検出点。"""
        fresh_act = _make_activity(title="fresh", status="in_progress")
        stale_act = _make_activity(title="stale", status="in_progress")
        _set_updated_at(stale_act, _days_ago(ov.DEFAULT_DAYS + 1))

        result = ov.get_overview()

        working_ids = {i["id_raw"] for i in result["working"]["items"]}
        assert working_ids == {fresh_act}
        assert result["working"]["total_count"] == 1
        assert result["backlog"]["stale_in_progress_count"] == 1
        assert result["backlog"]["by_status"]["in_progress"] == 1
        assert result["backlog"]["total_count"] == 1

    def test_max_of_updated_at_and_heartbeat_places_stale_updated_at_activity_in_working(self, temp_db):
        """updated_atは古いがheartbeatが新しい場合、max()判定によりworkingに載る。"""
        act = _make_activity(status="in_progress")
        _set_updated_at(act, _days_ago(30))
        # heartbeatはis_liveの閾値(既定20分)よりずっと古いが、days窓(7日)には収まる
        _set_heartbeat(act, _days_ago(1))

        result = ov.get_overview(days=7)

        working_ids = {i["id_raw"] for i in result["working"]["items"]}
        assert working_ids == {act}
        item = result["working"]["items"][0]
        assert item["is_live"] is False
        assert item["days_since_touch"] == 1

    def test_stale_in_progress_not_hot_lands_in_backlog(self, temp_db):
        act = _make_activity(status="in_progress")
        _set_updated_at(act, _days_ago(ov.DEFAULT_DAYS + 1))

        result = ov.get_overview()

        assert result["working"]["items"] == []
        assert result["backlog"]["stale_in_progress_count"] == 1

    def test_is_live_true_and_sorted_first_when_heartbeat_within_timeout(self, temp_db):
        not_live = _make_activity(title="not-live", status="in_progress")
        live = _make_activity(title="live", status="in_progress")
        _set_heartbeat(live, _minutes_ago(1))

        result = ov.get_overview()

        items = result["working"]["items"]
        assert items[0]["id_raw"] == live
        assert items[0]["is_live"] is True
        not_live_item = next(i for i in items if i["id_raw"] == not_live)
        assert not_live_item["is_live"] is False

    def test_open_ask_count_matches_number_of_blocking_open_asks(self, temp_db):
        act = _make_activity(status="in_progress")
        ask_service.add_ask("q1", tags=["domain:test"], blocks=[act])
        ask_service.add_ask("q2", tags=["domain:test"], blocks=[act])

        result = ov.get_overview()

        item = next(i for i in result["working"]["items"] if i["id_raw"] == act)
        assert item["open_ask_count"] == 2

    def test_pending_without_heartbeat_is_not_working(self, temp_db):
        """pendingは鮮度だけでは拾わない(_IS_LIVE側の分岐でしかworkingに入らない)。"""
        act = _make_activity(status="pending")

        result = ov.get_overview()

        assert result["working"]["items"] == []
        assert result["backlog"]["by_status"].get("pending") == 1


def _frozen_datetime_class(frozen_now: datetime) -> type:
    """datetime.now()だけfrozen_nowを返すサブクラスを作る(標準ライブラリのみでfreezeする)。

    overview_serviceはSQLの'now'リテラルではなく:nowバインドパラメータを使い、
    その値をPythonのdatetime.now(timezone.utc)から作る設計になっているため、
    このクラスでov.datetimeを差し替えるだけでSQL側の時刻計算も含めて固定できる。
    """
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now if tz is not None else frozen_now.replace(tzinfo=None)

    return _Frozen


class TestRecentlyDoneSection:
    def test_completed_within_days_included_with_correct_days_ago(self, temp_db, monkeypatch):
        """基準時刻をfreezeし、実時刻との差分ではなく固定値に対してdays_agoを検証する。"""
        frozen_now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        act = _make_activity(status="completed")
        _set_updated_at(act, _utc_str(frozen_now - timedelta(days=2)))

        monkeypatch.setattr(ov, "datetime", _frozen_datetime_class(frozen_now))

        result = ov.get_overview(days=7)

        assert result["generated_at"] == "2026-01-15 12:00:00"
        items = result["recently_done"]["items"]
        assert len(items) == 1
        assert items[0]["id_raw"] == act
        assert items[0]["status"] == "completed"
        assert items[0]["days_ago"] == 2

    def test_completed_outside_days_window_excluded(self, temp_db):
        act = _make_activity(status="completed")
        _set_updated_at(act, _days_ago(10))

        result = ov.get_overview(days=7)

        assert result["recently_done"]["items"] == []
        assert result["recently_done"]["total_count"] == 0

    def test_items_include_domains_field(self, temp_db):
        act = _make_activity(status="completed", tags=["domain:calm", "domain:infra"])

        result = ov.get_overview()

        item = result["recently_done"]["items"][0]
        assert sorted(item["domains"]) == ["calm", "infra"]


class TestAwaitingHumanSection:
    def test_open_ask_in_items_with_blocking_activity_title(self, temp_db):
        act = _make_activity(title="blocked act", status="in_progress")
        ask_id = ask_service.add_ask("need decision", tags=["domain:test"], blocks=[act])["id"]

        result = ov.get_overview()

        items = result["awaiting_human"]["items"]
        assert len(items) == 1
        assert items[0]["id_raw"] == ask_id
        assert items[0]["blocks"] == [{"id_raw": act, "title": "blocked act", "status": "in_progress"}]
        assert result["awaiting_human"]["triage_pending_count"] == 0

    def test_answered_untriaged_ask_excluded_from_items_but_counted_in_triage_pending(self, temp_db):
        act = _make_activity(status="in_progress")
        ask_id = ask_service.add_ask("q", tags=["domain:test"], blocks=[act])["id"]
        ask_service.answer_ask(ask_id, "answer body")

        result = ov.get_overview()

        assert result["awaiting_human"]["items"] == []
        assert result["awaiting_human"]["triage_pending_count"] == 1

    def test_days_open_uses_the_passed_reference_time(self, temp_db):
        """days_openは呼び出し元から渡されたnowを基準にする(内部で独自にdatetime.now()を読まない)。"""
        act = _make_activity(status="in_progress")
        ask_service.add_ask("q", tags=["domain:test"], blocks=[act])

        result = ov._collect_awaiting_human(
            limit=20, now=datetime.now(timezone.utc) + timedelta(days=5)
        )

        assert result["items"][0]["days_open"] == 5


class TestBacklogSection:
    def test_multi_domain_activity_counted_in_each_domain(self, temp_db):
        _make_activity(status="pending", tags=["domain:calm", "domain:infra"])

        result = ov.get_overview()

        by_domain = {row["domain"]: row["count"] for row in result["backlog"]["by_domain"]}
        assert by_domain == {"calm": 1, "infra": 1}
        assert result["backlog"]["total_count"] == 1

    def test_activity_without_domain_tag_counted_separately(self, temp_db):
        _make_activity(status="pending", tags=["intent:design"])

        result = ov.get_overview()

        assert result["backlog"]["by_domain"] == []
        assert result["backlog"]["no_domain_count"] == 1
        assert result["backlog"]["total_count"] == 1

    def test_alias_domain_tag_rolls_up_to_canonical_name(self, temp_db):
        """tag-cleanup後もbacklogのby_domainはcanonical名に寄せて集計する。"""
        _make_activity(status="pending", tags=["domain:calm"])
        _make_activity(status="pending", tags=["domain:calm-legacy"])
        # activity_tagsが既にcalm-legacyのtag_idを保持した状態でエイリアス化する
        # (ensure_tag_idsはリンク時にcanonicalへ解決するため、先にリンクしてから
        # canonicalを設定しないとLEFT JOIN側の分岐を通らない)
        update_tag("domain:calm-legacy", canonical="domain:calm")

        result = ov.get_overview()

        by_domain = {row["domain"]: row["count"] for row in result["backlog"]["by_domain"]}
        assert by_domain == {"calm": 2}
        assert result["backlog"]["total_count"] == 2


class TestOrchManagedNotFiltered:
    """orch_managedは旧ow運用体系の名残の死んだカラムであり、get_overviewは
    orch_managedによる除外フィルタを持たない(ユーザー裁定済み)。orch_managed=Trueの
    activityも他と全く同じ基準でworking/recently_done/backlogへ分類される。
    """

    def test_orch_managed_activities_are_classified_like_any_other(self, temp_db):
        orch_in_progress = _make_activity(status="in_progress", orch_managed=True)
        orch_completed = _make_activity(status="completed", orch_managed=True)
        _make_activity(status="pending", orch_managed=True)

        result = ov.get_overview()

        working_ids = {i["id_raw"] for i in result["working"]["items"]}
        done_ids = {i["id_raw"] for i in result["recently_done"]["items"]}

        assert orch_in_progress in working_ids
        assert orch_completed in done_ids
        assert result["working"]["total_count"] == 1
        assert result["recently_done"]["total_count"] == 1
        assert result["backlog"]["total_count"] == 1
        assert result["backlog"]["by_status"].get("pending") == 1

    def test_orch_managed_ask_and_blocked_activity_appear_in_awaiting_human(self, temp_db):
        """awaiting_humanもask_service.get_asksへそのまま委譲し、orch_managedで
        blocksを絞り込んだりask自体を落としたりしない。
        """
        act = _make_activity(title="orch blocked", status="in_progress", orch_managed=True)
        ask_id = ask_service.add_ask("need decision", tags=["domain:test"], blocks=[act])["id"]

        result = ov.get_overview()

        items = result["awaiting_human"]["items"]
        assert len(items) == 1
        assert items[0]["id_raw"] == ask_id
        assert items[0]["blocks"] == [{"id_raw": act, "title": "orch blocked", "status": "in_progress"}]


class TestWorkingBacklogStatusSymmetry:
    """snoozed/shelvedはworking対象statusではないため、heartbeatの生死に
    関わらず常にbacklogに残る(working/backlogのstatus条件が非対称だと、
    heartbeatだけ生きているsnoozed/shelvedがどちらの節にも現れなくなるバグの
    境界値回帰テスト)。
    """

    @pytest.mark.parametrize("status", ["snoozed", "shelved"])
    def test_live_heartbeat_still_lands_in_backlog_not_lost(self, temp_db, status):
        act = _make_activity(status=status)
        _set_heartbeat(act, _minutes_ago(1))  # HEARTBEAT_TIMEOUT_MINUTES(既定20分)以内=生きている

        result = ov.get_overview()

        working_ids = {i["id_raw"] for i in result["working"]["items"]}
        assert act not in working_ids
        assert result["backlog"]["total_count"] == 1
        assert result["backlog"]["by_status"].get(status) == 1

    @pytest.mark.parametrize("status", ["snoozed", "shelved"])
    def test_just_expired_heartbeat_also_lands_in_backlog(self, temp_db, status):
        act = _make_activity(status=status)
        # HEARTBEAT_TIMEOUT_MINUTESを1分超過=切れた直後
        _set_heartbeat(act, _minutes_ago(HEARTBEAT_TIMEOUT_MINUTES + 1))

        result = ov.get_overview()

        working_ids = {i["id_raw"] for i in result["working"]["items"]}
        assert act not in working_ids
        assert result["backlog"]["total_count"] == 1
        assert result["backlog"]["by_status"].get(status) == 1


class TestSideEffectFree:
    def test_snoozed_not_auto_revived_after_get_overview_call(self, temp_db):
        act = _make_activity(status="snoozed")
        _set_updated_at(act, _days_ago(SNOOZE_DURATION_DAYS + 5))

        ov.get_overview()

        assert _get_status(act) == "snoozed"


class TestLimitBehavior:
    def test_limit_below_population_truncates_items_but_total_count_reflects_full_population(self, temp_db):
        for i in range(5):
            _make_activity(title=f"w{i}", status="in_progress")

        result = ov.get_overview(limit=3)

        assert len(result["working"]["items"]) == 3
        assert result["working"]["count"] == 3
        assert result["working"]["total_count"] == 5

    def test_limit_over_max_is_clamped_to_100_and_applies_uniformly_to_all_sections(self, temp_db):
        total_rows = ov._MAX_LIMIT + 1
        for i in range(total_rows):
            act = _make_activity(title=f"w{i}", status="in_progress")
            ask_service.add_ask(f"question {i}", tags=["domain:test"], blocks=[act])

        result = ov.get_overview(limit=500)

        assert result["params"]["limit"] == ov._MAX_LIMIT
        assert result["working"]["count"] == ov._MAX_LIMIT
        assert result["working"]["total_count"] == total_rows
        assert result["awaiting_human"]["count"] == ov._MAX_LIMIT
        assert result["awaiting_human"]["total_count"] == total_rows


class TestSessionDataBoundary:
    def test_response_contains_no_session_identifier_fields(self, temp_db):
        act = _make_activity(status="in_progress")
        ask_service.add_ask("q", tags=["domain:test"], blocks=[act])

        result = ov.get_overview()

        forbidden_substrings = ("session", "alias")

        def _walk(obj):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    assert not any(f in key.lower() for f in forbidden_substrings), (
                        f"forbidden key found: {key}"
                    )
                    _walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    _walk(value)

        _walk(result)


class TestExhaustiveClassification:
    def test_non_completed_and_recently_completed_activities_are_classified_exactly_once(self, temp_db):
        """working/recently_done/backlogのいずれか1つに必ず分類される。

        backlogの対象statusはpending/in_progress/snoozed/shelvedのみで
        completedを含まないため、completedかつdays日超過のactivityはこの
        網羅性の対象外(仕様。recently_doneにもbacklogにも現れなくてよい)。
        本テストの母集団はcompletedをdays日以内のもの1件に絞ることで、
        網羅性が保証される範囲だけを検証する。
        """
        fresh_in_progress = _make_activity(status="in_progress")
        stale_in_progress = _make_activity(status="in_progress")
        _set_updated_at(stale_in_progress, _days_ago(ov.DEFAULT_DAYS + 1))
        pending_act = _make_activity(status="pending")
        snoozed_act = _make_activity(status="snoozed")
        # heartbeat生存中でもworking対象statusではないためbacklogに残ることを
        # この網羅性テストでも確認する(TestWorkingBacklogStatusSymmetryの単体
        # ケースと合わせて、母集団カウントの側でも回帰を検出できるようにする)
        _set_heartbeat(snoozed_act, _minutes_ago(1))
        shelved_act = _make_activity(status="shelved")
        recent_completed = _make_activity(status="completed")

        result = ov.get_overview(days=7, limit=100)

        working_ids = {i["id_raw"] for i in result["working"]["items"]}
        done_ids = {i["id_raw"] for i in result["recently_done"]["items"]}

        assert working_ids == {fresh_in_progress}
        assert done_ids == {recent_completed}
        assert working_ids.isdisjoint(done_ids)
        # backlogはitem単位を返さないため件数で検証する
        # (stale_in_progress, pending_act, snoozed_act, shelved_actの4件)
        assert result["backlog"]["total_count"] == 4
