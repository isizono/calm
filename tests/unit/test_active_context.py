"""_build_activities_section および関連ヘルパー関数のユニットテスト

データ取得関数はsrc/services/activity_service.pyに、
表示整形関数はhooks/session_start_hook.pyに配置されている。
"""
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from src import config
from src.db import get_connection
from src.services.topic_service import add_topic
from src.services.activity_service import (
    add_activity,
    update_activity,
    get_active_domains,
    get_active_activities_by_tag,
    get_pinned_active_activities,
)
from src.services.pin_service import add_pin
from src.services.ask_service import add_ask
from src.services import goal_service
import src.services.embedding_service as emb
from tests.helpers import add_decision
from hooks.session_start_hook import (
    _build_activities_section,
    _build_fixed_nav,
    _calc_elapsed_days,
    _DETERMINISTIC_RENDER_NOTICE,
    _LEGEND_LINE,
    _UNDISPLAYED_EXAMPLE_DOMAINS,
)

_NAV_BASE = (
    "作業開始時は該当アクティビティにcheck_in（なければ作成 — activity-start）。"
    "未表示や過去の文脈はget_activities・search・get系で取得する。"
)


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)



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
    """全アクティビティの created_at / updated_at を指定時間前に書き戻す。"""
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


def _set_updated_at_days_ago(activity_id: int, days: int) -> None:
    """指定activityのupdated_atをdays日前に書き換える（境界値テスト用）。"""
    conn = get_connection()
    try:
        past = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn.execute(
            "UPDATE activities SET updated_at = ? WHERE id = ?", (past, activity_id)
        )
        conn.commit()
    finally:
        conn.close()


def test_deterministic_render_notice_constant():
    """末尾固定文の文言が仕様通りである"""
    assert "決定論的に組み立てた表示用 markdown" in _DETERMINISTIC_RENDER_NOTICE
    assert "再フォーマットや優先順の再評価をせず" in _DETERMINISTIC_RENDER_NOTICE


def test_tier2_max_items_constant(monkeypatch):
    """環境変数が未設定なら階層 2 の上限は既定の5"""
    import importlib.util

    monkeypatch.delenv("CALM_TIER2_MAX_ITEMS", raising=False)
    spec = importlib.util.find_spec("src.config")
    fresh_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh_config)

    assert fresh_config.TIER2_MAX_ITEMS == 5


def test_tier2_max_items_reads_env_var(monkeypatch):
    """CALM_TIER2_MAX_ITEMSを設定してconfigを読み込むと、その値になる"""
    import importlib.util

    monkeypatch.setenv("CALM_TIER2_MAX_ITEMS", "10")
    spec = importlib.util.find_spec("src.config")
    fresh_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh_config)

    assert fresh_config.TIER2_MAX_ITEMS == 10


def test_tier2_max_items_negative_env_clamped_to_zero(monkeypatch):
    """負値を指定しても階層2の上限は0に丸まり、末尾スライスで意図と逆に出ない"""
    import importlib.util

    monkeypatch.setenv("CALM_TIER2_MAX_ITEMS", "-1")
    spec = importlib.util.find_spec("src.config")
    fresh_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh_config)

    assert fresh_config.TIER2_MAX_ITEMS == 0


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


def test_get_pinned_active_activities_returns_pinned(temp_db):
    result = add_activity(
        title="Pinned Activity", description="Desc",
        tags=["domain:pinproj"], check_in=False,
    )
    add_pin("tag", "domain:pinproj", "activity", result["activity_id"])
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
    add_pin("tag", "domain:pinproj", "activity", result["activity_id"])
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


class TestBuildFixedNav:
    """_build_fixed_nav（一覧末尾固定ナビ）のユニットテスト

    未表示の内訳（domain別件数・例示）は_build_undisplayed_linesが別途
    組み立てるため、本関数は引数を取らない固定文言を返すだけになった。
    """

    def test_returns_fixed_nav_base(self):
        """固定ナビは_NAV_BASEと完全一致する（パラメータ化廃止）"""
        assert _build_fixed_nav() == _NAV_BASE

    def test_no_direct_add_activity_wording(self):
        """activity-startスキル経由を案内し、add_activityで直接作成とは書かない"""
        nav = _build_fixed_nav()
        assert "activity-start" in nav
        assert "add_activityで直接作成" not in nav


class TestBuildActivitiesSectionEarlyReturn:
    """階層1・2とも0件のときの明示的early return"""

    def test_no_activities_returns_nav_only(self, temp_db):
        """activityが1件も無ければヘッダ・末尾注記なしで固定ナビのみ返す"""
        result = _build_active_context_wrapper()
        assert result == _NAV_BASE
        assert "# アクティビティ一覧" not in result

    def test_topics_only_returns_nav_only(self, temp_db):
        """トピックだけでアクティビティがない場合も固定ナビのみ"""
        add_topic(title="Topic Only", description="Desc", tags=["domain:myapp"])
        result = _build_active_context_wrapper()
        assert result == _NAV_BASE

    def test_pending_non_pinned_only_shows_undisplayed_section(self, temp_db):
        """pendingかつ非pinnedのみ（階層1・2とも0件）でも、未表示activityが
        1件でもあればヘッダ・未表示節・末尾固定文が出る（活動が1件も無い
        ときとは区別する）"""
        add_activity(
            title="[作業] 放置タスク", description="Desc",
            tags=["domain:myapp"], check_in=False,
        )
        result = _build_active_context_wrapper()
        assert "# アクティビティ一覧" in result
        assert "## 未表示 1件" in result
        assert "myapp 1件：[作業] 放置タスク" in result
        assert _DETERMINISTIC_RENDER_NOTICE in result
        # 階層1・2とも0件のため、ツリー記号を説明する凡例は出さない
        assert _LEGEND_LINE not in result


class TestTier2AgeBoundary:
    """階層2 in_progressアクティビティの7日境界"""

    def test_in_progress_6_days_shown(self, temp_db):
        r = add_activity(title="[作業] A", description="Desc", tags=["domain:myapp"], check_in=False)
        update_activity(r["activity_id"], status="in_progress")
        _set_updated_at_days_ago(r["activity_id"], 6)

        result = _build_active_context_wrapper()

        assert "## 優先" in result
        assert "[作業] A" in result

    def test_in_progress_7_days_boundary_shown(self, temp_db):
        r = add_activity(title="[作業] A", description="Desc", tags=["domain:myapp"], check_in=False)
        update_activity(r["activity_id"], status="in_progress")
        _set_updated_at_days_ago(r["activity_id"], 7)

        result = _build_active_context_wrapper()

        assert "## 優先" in result
        assert "[作業] A" in result

    def test_in_progress_8_days_hidden(self, temp_db):
        r = add_activity(title="[作業] A", description="Desc", tags=["domain:myapp"], check_in=False)
        update_activity(r["activity_id"], status="in_progress")
        _set_updated_at_days_ago(r["activity_id"], 8)

        result = _build_active_context_wrapper()

        assert "## 優先" not in result
        assert "myapp 1件：[作業] A" in result


class TestTier2PinnedDecay:
    """pinnedアクティビティの7日フィルタ免除と60日decay"""

    def test_pending_pinned_within_decay_shown(self, temp_db):
        """statusがpendingでもpinnedなら7日フィルタを免除され表示される"""
        r = add_activity(title="[作業] Pinned Pending", description="Desc", tags=["domain:myapp"], check_in=False)
        add_pin("tag", "domain:myapp", "activity", r["activity_id"])
        _set_updated_at_days_ago(r["activity_id"], 10)

        result = _build_active_context_wrapper()

        assert "[作業] Pinned Pending" in result

    def test_pinned_60_days_boundary_shown(self, temp_db):
        r = add_activity(title="[作業] B", description="Desc", tags=["domain:myapp"], check_in=False)
        add_pin("tag", "domain:myapp", "activity", r["activity_id"])
        _set_updated_at_days_ago(r["activity_id"], 60)

        result = _build_active_context_wrapper()

        assert "[作業] B" in result

    def test_pinned_61_days_decays_out_of_tier2(self, temp_db):
        """pinnedでも60日超のupdated_atは階層2から外れ、未表示のdomain内訳に計上される"""
        r = add_activity(title="[作業] C", description="Desc", tags=["domain:myapp"], check_in=False)
        add_pin("tag", "domain:myapp", "activity", r["activity_id"])
        _set_updated_at_days_ago(r["activity_id"], 61)

        result = _build_active_context_wrapper()

        assert "## 優先" not in result
        assert "myapp 1件：[作業] C" in result

    def test_pinned_decay_does_not_remove_pin_itself(self, temp_db):
        """60日decayでpinが階層2から落ちても、pinned一覧からは消えない（pin自体は残る）"""
        r = add_activity(title="[作業] D", description="Desc", tags=["domain:myapp"], check_in=False)
        add_pin("tag", "domain:myapp", "activity", r["activity_id"])
        _set_updated_at_days_ago(r["activity_id"], 61)

        pinned = get_pinned_active_activities()
        ids = [a["id"] for a in pinned]
        assert r["activity_id"] in ids


class TestNoStatsLine:
    """旧階層3/4の統計行が出力に含まれないことの確認"""

    def test_no_recent_24h_stats_line(self, temp_db):
        add_activity(title="[作業] Task", description="Desc", tags=["domain:myapp"], check_in=False)
        result = _build_active_context_wrapper()
        assert "直近24h" not in result

    def test_no_30days_stats_line(self, temp_db):
        add_activity(title="[作業] Task", description="Desc", tags=["domain:myapp"], check_in=False)
        result = _build_active_context_wrapper()
        assert "30日以内" not in result

    def test_no_other_summary_prefix(self, temp_db):
        for i in range(3):
            add_activity(
                title=f"[作業] Activity {i}", description="Desc",
                tags=["domain:myapp"], check_in=False,
            )
        result = _build_active_context_wrapper()
        assert "他:" not in result


class TestUndisplayedSection:
    """末尾『未表示』節: domain別件数と直近更新順2件の例示

    決定事項「未表示はdomain別の件数と各2件の例で出す」の実装。
    """

    def test_undisplayed_heading_count_matches_population(self, temp_db):
        """見出しの件数は「母集団（active全件）−表示済み件数」に一致する"""
        r1 = add_activity(title="[作業] Shown", description="Desc", tags=["domain:myapp"], check_in=False)
        update_activity(r1["activity_id"], status="in_progress")
        add_activity(title="[作業] Hidden1", description="Desc", tags=["domain:myapp"], check_in=False)
        add_activity(title="[作業] Hidden2", description="Desc", tags=["domain:myapp"], check_in=False)

        result = _build_active_context_wrapper()

        assert "[作業] Shown" in result
        assert "## 未表示 2件" in result
        assert "myapp 2件：" in result

    def test_zero_undisplayed_omits_section(self, temp_db):
        """未表示が0件なら『未表示』節自体が出ない"""
        r1 = add_activity(title="[作業] Only", description="Desc", tags=["domain:myapp"], check_in=False)
        update_activity(r1["activity_id"], status="in_progress")

        result = _build_active_context_wrapper()

        assert "## 未表示" not in result

    def test_examples_capped_at_two_with_suffix(self, temp_db):
        """domain内の例示は直近更新順で2件までにし、まだ隠れた項目があるときだけ
        末尾に「など」を付ける"""
        for i in range(3):
            add_activity(
                title=f"[作業] Hidden{i}", description="Desc",
                tags=["domain:myapp"], check_in=False,
            )

        result = _build_active_context_wrapper()

        assert "## 未表示 3件" in result
        line = next(l for l in result.splitlines() if l.startswith("- myapp"))
        assert line.count("[作業] Hidden") == 2
        assert line.endswith("など")

    def test_examples_all_shown_omits_suffix(self, temp_db):
        """domain内の未表示が2件以内で例示に全件収まるときは「など」を付けない"""
        add_activity(title="[作業] Solo", description="Desc", tags=["domain:myapp"], check_in=False)

        result = _build_active_context_wrapper()

        line = next(l for l in result.splitlines() if l.startswith("- myapp"))
        assert line == "- myapp 1件：[作業] Solo"
        assert "など" not in line

    def test_domains_ordered_by_count_descending(self, temp_db):
        """件数の多いdomainから並べる"""
        for i in range(3):
            add_activity(title=f"[作業] Big{i}", description="Desc", tags=["domain:big"], check_in=False)
        add_activity(title="[作業] Small0", description="Desc", tags=["domain:small"], check_in=False)

        result = _build_active_context_wrapper()

        idx_section = result.index("## 未表示")
        idx_big = result.index("- big", idx_section)
        idx_small = result.index("- small", idx_section)
        assert idx_big < idx_small

    def _add_domain_activities(self, name: str, n: int) -> None:
        for i in range(n):
            add_activity(
                title=f"[作業] {name}-{i}", description="Desc",
                tags=[f"domain:{name}"], check_in=False,
            )

    @pytest.mark.parametrize(
        "counts",
        [
            [("d0", 3), ("d1", 2), ("d2", 1)],
            [("d0", 4), ("d1", 3), ("d2", 2), ("d3", 1)],
        ],
        ids=["3domains", "4domains"],
    )
    def test_four_or_fewer_domains_no_fold(self, temp_db, counts):
        """domainが4個以下なら、まとめ行が出ず、全domainが例示付きで出る"""
        for name, n in counts:
            self._add_domain_activities(name, n)

        result = _build_active_context_wrapper()

        assert "ほか" not in result
        for name, n in counts:
            assert f"- {name} {n}件：" in result

    def test_five_domains_folds_to_top_three_plus_summary(self, temp_db):
        """5 domain（件数がすべて異なる）なら、例示行がちょうど3行、件数の
        多い順に出て、そのあとにまとめ行が1行出る"""
        counts = [("d0", 5), ("d1", 4), ("d2", 3), ("d3", 2), ("d4", 1)]
        for name, n in counts:
            self._add_domain_activities(name, n)

        result = _build_active_context_wrapper()

        assert "## 未表示 15件" in result
        section = result[result.index("## 未表示"):]
        example_lines = [
            line for line in section.splitlines()
            if line.startswith("- ") and not line.startswith("- ほか")
        ]
        assert len(example_lines) == _UNDISPLAYED_EXAMPLE_DOMAINS
        assert [line.split(" ")[1] for line in example_lines] == ["d0", "d1", "d2"]

        summary_line = next(line for line in section.splitlines() if line.startswith("- ほか"))
        assert summary_line == "- ほか2 domain：d3 2件、d4 1件"

    def test_undisplayed_heading_unaffected_by_folding(self, temp_db):
        """未表示の見出し総数は、畳んでも畳まなくても変わらない"""
        counts = [("d0", 3), ("d1", 2), ("d2", 2), ("d3", 1), ("d4", 1)]
        for name, n in counts:
            self._add_domain_activities(name, n)

        result = _build_active_context_wrapper()

        assert "## 未表示 9件" in result

    def test_many_domains_summary_lists_all_without_truncation_within_budget(self, temp_db):
        """まとめ行に、例示されなかったdomainが件数の多い順で漏れなく出る
        （30 domainでも、どのdomain名も一覧から消えない）。組み立て結果は
        既定予算4000字に収まり、固定ナビで終わる（切り詰めの印が出ない）"""
        top = [("t0", 5), ("t1", 4), ("t2", 3)]
        # 偶奇で件数を入れ違いに作り、まとめ行がdomain作成順ではなく
        # 件数降順で並ぶことを検証する
        rest = [(f"r{i:02d}", 2 if i % 2 == 0 else 1) for i in range(27)]
        for name, n in top + rest:
            self._add_domain_activities(name, n)

        result = _build_active_context_wrapper()

        total = sum(n for _, n in top + rest)
        assert f"## 未表示 {total}件" in result

        section = result[result.index("## 未表示"):]
        summary_line = next(line for line in section.splitlines() if line.startswith("- ほか"))
        assert summary_line.startswith(f"- ほか{len(rest)} domain：")
        for name, _ in rest:
            assert name in summary_line

        entries = summary_line.split("：", 1)[1].split("、")
        summary_counts = [int(entry.split(" ")[-1].rstrip("件")) for entry in entries]
        assert summary_counts == sorted(summary_counts, reverse=True)

        assert len(result) <= config.INJECTION_BUDGET_ACTIVITIES_CHARS
        assert "切り詰め" not in result
        assert result.endswith(_build_fixed_nav() + "\n")


def test_build_activities_section_no_topic_section(temp_db):
    """旧トピックセクション（最新トピック:）は出力されない"""
    add_topic(title="My Topic", description="Desc", tags=["domain:myapp"])
    r = add_activity(title="[作業] 実装する", description="Desc", tags=["domain:myapp"], check_in=False)
    update_activity(r["activity_id"], status="in_progress")

    result = _build_active_context_wrapper()

    assert "最新トピック:" not in result
    assert "My Topic" not in result


def test_build_activities_section_tier2_capped_at_five(temp_db):
    """階層 2『優先』は上位 5 件までに絞られ、残りは未表示に回る"""
    for i in range(7):
        r = add_activity(
            title=f"[作業] Activity {i}", description="Desc",
            tags=["domain:myapp"], check_in=False,
        )
        update_activity(r["activity_id"], status="in_progress")

    result = _build_active_context_wrapper()

    idx_tier2 = result.index("## 優先")
    next_section = result.find("\n## ", idx_tier2 + 1)
    tier2_block = result[idx_tier2:] if next_section == -1 else result[idx_tier2:next_section]
    shown = [line for line in tier2_block.splitlines() if line.startswith("- #")]
    assert len(shown) == 5
    assert "## 未表示 2件" in result


def test_build_activities_section_tier2_max_items_env_override(temp_db, monkeypatch):
    """config.TIER2_MAX_ITEMSを増やすと、階層2の表示件数も連動して増える"""
    monkeypatch.setattr(config, "TIER2_MAX_ITEMS", 10)
    for i in range(12):
        r = add_activity(
            title=f"[作業] Activity {i}", description="Desc",
            tags=["domain:myapp"], check_in=False,
        )
        update_activity(r["activity_id"], status="in_progress")

    result = _build_active_context_wrapper()

    idx_tier2 = result.index("## 優先")
    next_section = result.find("\n## ", idx_tier2 + 1)
    tier2_block = result[idx_tier2:] if next_section == -1 else result[idx_tier2:next_section]
    shown = [line for line in tier2_block.splitlines() if line.startswith("- #")]
    assert len(shown) == 10
    assert "## 未表示 2件" in result


def test_build_activities_section_domain_with_zero_activities_skipped(temp_db):
    """アクティビティ0件のdomainセクションはどこにも出現しない"""
    r = add_activity(title="Activity", description="Desc", tags=["domain:myapp"], check_in=False)
    update_activity(r["activity_id"], status="in_progress")

    conn = get_connection()
    try:
        from src.services.tag_service import ensure_tag_ids
        ensure_tag_ids(conn, [("domain", "empty-domain")])
        conn.commit()
    finally:
        conn.close()

    result = _build_active_context_wrapper()

    assert "empty-domain" not in result


def test_build_activities_section_activity_id_in_bracket(temp_db):
    """アクティビティIDが「#NNN title」形式で表示される（個別表示は階層 2 のみ対象）"""
    activity = add_activity(title="Sample Task", description="Desc", tags=["domain:myapp"], check_in=False)
    activity_id = activity["activity_id"]
    update_activity(activity_id, status="in_progress")

    result = _build_active_context_wrapper()

    assert f"#{activity_id} Sample Task" in result


def test_build_activities_section_raises_on_invalid_db(temp_db):
    """DB接続失敗時は例外が発生する（hookのmain()がcatchする前提）"""
    from src.env_compat import env_set

    os.environ["DISCUSSION_DB_PATH"] = "/nonexistent/path/test.db"
    # temp_dbフィクスチャが設定したCALM_DB_PATHが残っていると、DISCUSSION_DB_PATH
    # より優先されてこの無効パスへの差し替えが素通りしてしまうため、同時に上書きする。
    env_set("CALM_DB_PATH", "/nonexistent/path/test.db")

    with pytest.raises(Exception):
        _build_active_context_wrapper()

    os.environ["DISCUSSION_DB_PATH"] = temp_db
    env_set("CALM_DB_PATH", temp_db)


def test_build_activities_section_completed_activities_excluded(temp_db):
    """completedアクティビティは表示されない"""
    result = add_activity(title="Done Activity", description="Desc", tags=["domain:myapp"], check_in=False)
    update_activity(result["activity_id"], status="completed")

    ctx = _build_active_context_wrapper()

    assert "Done Activity" not in ctx


def test_build_activities_section_deterministic_render_notice(temp_db):
    """階層1/2のいずれかが1件以上あれば末尾固定文が付く"""
    r = add_activity(title="[作業] Task", description="Desc", tags=["domain:myapp"], check_in=False)
    update_activity(r["activity_id"], status="in_progress")

    result = _build_active_context_wrapper()

    assert "決定論的に組み立てた表示用 markdown" in result


def test_build_activities_section_no_scoring_instructions(temp_db):
    """旧スコアリング指示文は出力されない"""
    r = add_activity(title="[作業] Task", description="Desc", tags=["domain:myapp"], check_in=False)
    update_activity(r["activity_id"], status="in_progress")

    result = _build_active_context_wrapper()

    assert "# スコアリング指示" not in result
    assert "上位5件を選び" not in result
    assert "depends_on未完了" not in result


def test_build_activities_section_no_tags_metadata(temp_db):
    """新仕様では tags meta 行は出力されない"""
    topic = add_topic(title="t", description="d", tags=["domain:myapp"])
    dec = add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
    r = add_activity(
        title="[作業] Task", description="Desc",
        tags=["domain:myapp", "intent:implement"],
        related=[{"type": "decision", "ids": [dec["decision_id"]]}],
        check_in=False,
    )
    update_activity(r["activity_id"], status="in_progress")

    result = _build_active_context_wrapper()

    assert "tags:" not in result


def test_build_activities_section_no_description_snippet(temp_db):
    """新仕様では desc snippet 行は出力されない"""
    r = add_activity(
        title="[作業] Task", description="締め切りは来週金曜日",
        tags=["domain:myapp"], check_in=False,
    )
    update_activity(r["activity_id"], status="in_progress")

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

    assert "blocked_by:" not in result


def test_build_activities_section_deduplicates_multi_domain(temp_db):
    """複数domainに属するアクティビティは未表示の見出し件数でも1件として重複なく数えられる
    （domain別の内訳では両方のdomainに例示されてよい）"""
    add_activity(
        title="Multi Domain Task", description="Desc",
        tags=["domain:app", "domain:lib"], check_in=False,
    )

    result = _build_active_context_wrapper()

    assert "## 未表示 1件" in result
    assert "app 1件：Multi Domain Task" in result
    assert "lib 1件：Multi Domain Task" in result


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
    assert f"- #{r1['activity_id']} [議論] stop_hookのスキップ機能" in tier2_block


class TestOrchChildTree:
    """goal_conditions（bound_type='activity'）から組み立てる親子ツリーのテスト

    決定事項「起動時の一覧でorchの子を親の下に線でぶら下げる」
    「一覧の状態記号は ✓ ▷ ◷ ✕ にする」
    「止まっている子を『待ち』と『着手できる』に分ける」の実装を検証する。
    """

    def _bind_children(self, parent_id, conditions):
        result = goal_service.set_goal(
            parent_id,
            {"new": {"handle": f"test-goal-{parent_id}", "statement": "test", "conditions": conditions}},
        )
        assert "error" not in result
        return result

    def test_achieved_child_counted_not_rendered(self, temp_db):
        """satisfied条件の子は✓の内訳数に入り、ツリー行としては出ない"""
        parent = add_activity(title="[統合] 親A", description="d", tags=["domain:myapp"], check_in=False)
        update_activity(parent["activity_id"], status="in_progress")
        child = add_activity(title="[作業] 子済", description="d", tags=["domain:myapp"], check_in=False)

        self._bind_children(parent["activity_id"], [
            {
                "statement": "子済が終わった", "actor": "claude", "state": "satisfied",
                "bound": {"type": "activity", "id": child["activity_id"]},
            },
        ])

        result = _build_active_context_wrapper()

        assert f"#{parent['activity_id']} [統合] 親A  ✓1" in result
        assert f"#{child['activity_id']}" not in result

    def test_achieved_child_still_active_stays_visible(self, temp_db):
        """束縛条件がsatisfiedになっても、子自身のactivityが非completedのまま
        in_progressで残っていれば、通常のactivityとして一覧から消えない
        （条件の充足と子自身の終了は別操作であるため）"""
        parent = add_activity(title="[統合] 親H", description="d", tags=["domain:myapp"], check_in=False)
        update_activity(parent["activity_id"], status="in_progress")
        child = add_activity(title="[作業] 条件だけ済んだ子", description="d", tags=["domain:myapp"], check_in=False)
        update_activity(child["activity_id"], status="in_progress")

        self._bind_children(parent["activity_id"], [
            {
                "statement": "子が終わった", "actor": "claude", "state": "satisfied",
                "bound": {"type": "activity", "id": child["activity_id"]},
            },
        ])

        result = _build_active_context_wrapper()

        assert f"- #{child['activity_id']} [作業] 条件だけ済んだ子" in result

    def test_never_active_open_child_marked_ready(self, temp_db):
        """openな子でheartbeat無し（一度も動いていない）は▷（着手できる）として
        `|` `└-` でぶら下がる"""
        parent = add_activity(title="[統合] 親B", description="d", tags=["domain:myapp"], check_in=False)
        update_activity(parent["activity_id"], status="in_progress")
        child = add_activity(title="[作業] 子未着手", description="d", tags=["domain:myapp"], check_in=False)

        self._bind_children(parent["activity_id"], [
            {
                "statement": "子未着手が終わる", "actor": "claude",
                "bound": {"type": "activity", "id": child["activity_id"]},
            },
        ])

        result = _build_active_context_wrapper()

        idx_tier2 = result.index("## 優先")
        tier2_block = result[idx_tier2:]
        assert "  |" in tier2_block
        assert f"└- ▷ #{child['activity_id']} [作業] 子未着手" in tier2_block
        assert "▷1" in tier2_block

    def test_open_ask_marks_child_waiting(self, temp_db):
        """子を止めるopen askがあれば◷（待ち）になる"""
        parent = add_activity(title="[統合] 親C", description="d", tags=["domain:myapp"], check_in=False)
        update_activity(parent["activity_id"], status="in_progress")
        child = add_activity(title="[作業] ask待ち子", description="d", tags=["domain:myapp"], check_in=False)

        self._bind_children(parent["activity_id"], [
            {
                "statement": "ask待ち子が終わる", "actor": "claude",
                "bound": {"type": "activity", "id": child["activity_id"]},
            },
        ])
        add_ask("これは判断が要る", blocks=[child["activity_id"]], tags=["domain:myapp"], notify=False)

        result = _build_active_context_wrapper()

        idx_tier2 = result.index("## 優先")
        tier2_block = result[idx_tier2:]
        assert f"◷ #{child['activity_id']}" in tier2_block
        assert "◷1" in tier2_block

    def test_unresolved_dependency_marks_child_waiting(self, temp_db):
        """未完了のdepends_on先があれば◷（待ち）になり、blocked_byも出す"""
        parent = add_activity(title="[統合] 親D", description="d", tags=["domain:myapp"], check_in=False)
        update_activity(parent["activity_id"], status="in_progress")
        blocker = add_activity(title="[作業] 依存先", description="d", tags=["domain:myapp"], check_in=False)
        child = add_activity(title="[作業] 依存待ち子", description="d", tags=["domain:myapp"], check_in=False)

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (child["activity_id"], blocker["activity_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        self._bind_children(parent["activity_id"], [
            {
                "statement": "依存待ち子が終わる", "actor": "claude",
                "bound": {"type": "activity", "id": child["activity_id"]},
            },
        ])

        result = _build_active_context_wrapper()

        idx_tier2 = result.index("## 優先")
        tier2_block = result[idx_tier2:]
        assert f"◷ #{child['activity_id']}" in tier2_block
        assert "blocked_by:" in tier2_block
        assert "依存先" in tier2_block

    def test_child_own_human_condition_marks_waiting(self, temp_db):
        """子自身のgoalにactorがhumanのopen条件があれば◷（待ち）になる"""
        parent = add_activity(title="[統合] 親E", description="d", tags=["domain:myapp"], check_in=False)
        update_activity(parent["activity_id"], status="in_progress")
        child = add_activity(title="[作業] 人間待ち子", description="d", tags=["domain:myapp"], check_in=False)

        own_goal_result = goal_service.set_goal(
            child["activity_id"],
            {
                "new": {
                    "handle": "test-child-own-goal",
                    "statement": "test",
                    "conditions": [{"statement": "ユーザーの承認", "actor": "human"}],
                }
            },
        )
        assert "error" not in own_goal_result

        self._bind_children(parent["activity_id"], [
            {
                "statement": "人間待ち子が終わる", "actor": "claude",
                "bound": {"type": "activity", "id": child["activity_id"]},
            },
        ])

        result = _build_active_context_wrapper()

        idx_tier2 = result.index("## 優先")
        tier2_block = result[idx_tier2:]
        assert f"◷ #{child['activity_id']}" in tier2_block

    def test_failed_child_goal_marks_failed(self, temp_db):
        """子自身のgoalがfailedで閉じ、親の条件がまだopenなら✕（失敗）になる"""
        parent = add_activity(title="[統合] 親F", description="d", tags=["domain:myapp"], check_in=False)
        update_activity(parent["activity_id"], status="in_progress")
        child = add_activity(title="[作業] 失敗子", description="d", tags=["domain:myapp"], check_in=False)

        own_goal_result = goal_service.set_goal(
            child["activity_id"],
            {
                "new": {
                    "handle": "test-child-failed-goal",
                    "statement": "test",
                    "conditions": [{"statement": "何かをする", "actor": "claude"}],
                }
            },
        )
        assert "error" not in own_goal_result
        judge_result = goal_service.judge_goal(
            own_goal_result["goal"]["goal_id_raw"], "failed", note="うまくいかなかった"
        )
        assert "error" not in judge_result

        self._bind_children(parent["activity_id"], [
            {
                "statement": "失敗子が終わる", "actor": "claude",
                "bound": {"type": "activity", "id": child["activity_id"]},
            },
        ])

        result = _build_active_context_wrapper()

        idx_tier2 = result.index("## 優先")
        tier2_block = result[idx_tier2:]
        assert f"✕ #{child['activity_id']}" in tier2_block
        assert "✕1" in tier2_block

    def test_open_child_excluded_from_flat_pool_and_undisplayed(self, temp_db):
        """子は優先の上位5件の枠から独立には出ず、親の下にのみ出る。
        表示された親の下の子は未表示にも数えない"""
        parent = add_activity(title="[統合] 親G", description="d", tags=["domain:myapp"], check_in=False)
        update_activity(parent["activity_id"], status="in_progress")
        child = add_activity(title="[作業] 除外確認子", description="d", tags=["domain:myapp"], check_in=False)
        update_activity(child["activity_id"], status="in_progress")

        self._bind_children(parent["activity_id"], [
            {
                "statement": "除外確認子が終わる", "actor": "claude",
                "bound": {"type": "activity", "id": child["activity_id"]},
            },
        ])

        result = _build_active_context_wrapper()

        assert result.count(f"#{child['activity_id']}") == 1
        assert f"- #{child['activity_id']}" not in result
        assert "## 未表示" not in result

    def test_unresolved_deps_not_queried_twice_for_open_child(self, temp_db):
        """未完了の子のdepends_on問い合わせは、blocked_by用のバッチ取得と
        _classify_children内の判定とで重複して発行されない（N+1回避の契約）"""
        import hooks.session_start_hook as hook_module

        parent = add_activity(title="[統合] 親I", description="d", tags=["domain:myapp"], check_in=False)
        update_activity(parent["activity_id"], status="in_progress")
        child = add_activity(title="[作業] 重複確認子", description="d", tags=["domain:myapp"], check_in=False)

        self._bind_children(parent["activity_id"], [
            {
                "statement": "重複確認子が終わる", "actor": "claude",
                "bound": {"type": "activity", "id": child["activity_id"]},
            },
        ])

        with patch.object(
            hook_module, "_get_unresolved_deps", wraps=hook_module._get_unresolved_deps
        ) as spy:
            _build_active_context_wrapper()
            assert spy.call_count == 1

    def test_children_suffix_order_matches_legend(self, temp_db):
        """親の行末尾の内訳は凡例と同じ並び（✓達成 ▷着手できる ◷待ち ✕失敗）で出る"""
        parent = add_activity(title="[統合] 親J", description="d", tags=["domain:myapp"], check_in=False)
        update_activity(parent["activity_id"], status="in_progress")
        achieved_child = add_activity(title="[作業] 済子", description="d", tags=["domain:myapp"], check_in=False)
        ready_child = add_activity(title="[作業] 着手できる子", description="d", tags=["domain:myapp"], check_in=False)
        waiting_child = add_activity(title="[作業] 待ち子", description="d", tags=["domain:myapp"], check_in=False)

        self._bind_children(parent["activity_id"], [
            {
                "statement": "済子が終わった", "actor": "claude", "state": "satisfied",
                "bound": {"type": "activity", "id": achieved_child["activity_id"]},
            },
            {
                "statement": "着手できる子が終わる", "actor": "claude",
                "bound": {"type": "activity", "id": ready_child["activity_id"]},
            },
            {
                "statement": "待ち子が終わる", "actor": "claude",
                "bound": {"type": "activity", "id": waiting_child["activity_id"]},
            },
        ])
        add_ask("これは判断が要る", blocks=[waiting_child["activity_id"]], tags=["domain:myapp"], notify=False)

        result = _build_active_context_wrapper()

        assert f"#{parent['activity_id']} [統合] 親J  ✓1 ▷1 ◷1" in result
