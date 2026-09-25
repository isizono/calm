"""checkin_tier_service.collect_and_assembleの統合テスト。

check_inの応答をtier形（anchor/control/context/catalog/env）で検証する。
基本的な収集ロジック（pinned・関連トピック・coverage・logsカタログ・
dependencies・recomposeナッジ・セッション別名登録・goal配線）に加え、
上限畳み（asks/dependencies）、goal失敗時の隔離、completedアクティビティの
再開順序など、tier実装固有の振る舞いを確認する。
"""
import json
import pytest

import src.services.checkin_tier_service as checkin_tier_service
from src.db import get_connection
from src.infra import session_identity
from src.services import goal_service as gs
from src.services import session_ledger_service, session_registry_service
from src.services.activity_service import add_activity, update_activity
from src.services.ask_service import add_ask_with_conn
from src.services.checkin_queries import DECISIONS_FULL_LIMIT
from src.services.checkin_tier_service import collect_and_assemble
from src.services.hint_service import (
    ACTIVITY_CLEANUP_AUTOTRIGGER_GUARD,
    ACTIVITY_CLEANUP_COUNT_THRESHOLD,
    MARKER_ACTIVITY_CLEANUP,
    MARKER_RECOMPOSE_BOOTSTRAP,
    RECOMPOSE_BOOTSTRAP_THRESHOLD as _RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD,
    RECOMPOSE_DELTA_THRESHOLD as _RECOMPOSE_HINT_DELTA_THRESHOLD,
)
from src.services.material_service import add_material
from src.services.pin_service import add_pin
from src.services.relation_service import add_relation
from src.services.topic_service import add_topic
from tests.helpers import add_decision, add_log, retract_decision

DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def activity_id(temp_db):
    """テスト用アクティビティを作成してIDを返すフィクスチャ"""
    result = add_activity(
        title="[作業] タグnotesカラム追加",
        description="タグnotesカラムを追加する作業",
        tags=DEFAULT_TAGS,
        check_in=False,
    )
    return result["activity_id"]


@pytest.fixture
def activity_with_intent(temp_db):
    """intent:タグ付きアクティビティを作成するフィクスチャ"""
    result = add_activity(
        title="[設計] API設計",
        description="APIの設計を行う",
        tags=["domain:test", "intent:design"],
        check_in=False,
    )
    return result["activity_id"]


class TestCheckIn:
    """collect_and_assembleの統合テスト"""

    def test_check_in_success(self, activity_id):
        """check-inが成功し、必須フィールドがすべて返る"""
        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert result["anchor"]["activity"]["id_raw"] == activity_id
        assert result["anchor"]["activity"]["title"] == "[作業] タグnotesカラム追加"
        assert result["anchor"]["activity"]["description"] == "タグnotesカラムを追加する作業"
        assert result["anchor"]["activity"]["status"] == "in_progress"
        assert "tags" in result["anchor"]["activity"]
        assert "coverage" in result["env"]
        assert "session" in result["env"]
        assert "goal" in result["control"]

    def test_check_in_status_updated_to_in_progress(self, activity_id):
        """pendingのアクティビティがin_progressに自動更新される"""
        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert result["anchor"]["activity"]["status"] == "in_progress"

    def test_check_in_already_in_progress(self, activity_id):
        """すでにin_progressの場合、status変更なしでcheck-in成功"""
        update_activity(activity_id, status="in_progress")

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert result["anchor"]["activity"]["status"] == "in_progress"

    def test_check_in_completed_activity(self, activity_id):
        """completedのアクティビティもin_progressに戻る"""
        update_activity(activity_id, status="completed")

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert result["anchor"]["activity"]["status"] == "in_progress"

    def test_check_in_not_found(self, temp_db):
        """存在しないactivity_idでNOT_FOUNDエラーになる"""
        result = collect_and_assemble(9999)

        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"
        assert "9999" in result["error"]["message"]

    def test_check_in_no_related_topics_when_no_relations(self, activity_id):
        """リレーションがない場合、context.topicsが結果に含まれない"""
        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "topics" not in result.get("context", {})

    def test_check_in_materials_empty(self, activity_id):
        """materialsが無い場合、context.materialsキーは省略される"""
        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "materials" not in result.get("context", {})

    def test_check_in_with_materials(self, activity_id):
        """materialsがある場合、relationsテーブル経由でカタログ形式で返る"""
        add_material("設計書", "# 設計\n詳細内容", ["domain:test"], "テスト用データ",
                     related=[{"type": "activity", "ids": [activity_id]}])
        add_material("調査結果", "# 調査\n結果内容", ["domain:test"], "テスト用データ",
                     related=[{"type": "activity", "ids": [activity_id]}])

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        materials = result["context"]["materials"]
        assert len(materials) == 2
        # カタログ形式: id_raw, title, snippet, source, created_at (contentなし)
        for m in materials:
            assert "id_raw" in m
            assert "id" not in m
            assert "title" in m
            assert "snippet" in m
            assert "source" in m
            assert "created_at" in m
            assert "content" not in m
            assert m["source"] == "テスト用データ"
        assert materials[0]["snippet"] == "# 設計\n詳細内容"
        assert materials[1]["snippet"] == "# 調査\n結果内容"

    def test_check_in_materials_snippet_truncated(self, activity_id):
        """materialsのsnippetが200文字に切り詰められる"""
        long_content = "あ" * 250
        add_material("長い資材", long_content, ["domain:test"], "テスト用データ",
                      related=[{"type": "activity", "ids": [activity_id]}])

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        materials = result["context"]["materials"]
        assert len(materials) == 1
        assert len(materials[0]["snippet"]) == 200
        assert materials[0]["snippet"] == "あ" * 200

    def test_check_in_recent_decisions_empty_without_relations(self, activity_id):
        """リレーションがない場合、context.decisionsキーは省略される"""
        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "decisions" not in result.get("context", {})


class TestCheckInFlowGuide:
    """flow_guide（セッション内初回のみのコンテキスト取得フローガイド）の確認"""

    def test_flow_guide_present_on_first_call(self, activity_id):
        """セッション内初回のcheck_inではenv.flow_guideが含まれる"""
        result = collect_and_assemble(activity_id, session_id="sess-1")

        assert "error" not in result
        assert "flow_guide" in result["env"]
        assert "get_decisions" in result["env"]["flow_guide"]

    def test_flow_guide_absent_on_second_call_same_session(self, activity_id):
        """同一セッションの2回目以降のcheck_inではenv.flow_guideが含まれない"""
        collect_and_assemble(activity_id, session_id="sess-1")
        result = collect_and_assemble(activity_id, session_id="sess-1")

        assert "error" not in result
        assert "flow_guide" not in result["env"]

    def test_flow_guide_present_again_for_different_session(self, activity_id):
        """異なるセッションではそれぞれ初回にenv.flow_guideが含まれる"""
        collect_and_assemble(activity_id, session_id="sess-1")
        result = collect_and_assemble(activity_id, session_id="sess-2")

        assert "error" not in result
        assert "flow_guide" in result["env"]

    def test_flow_guide_present_every_call_without_session_id(self, activity_id):
        """session_id未解決（None）では記録を読み書きしないため、
        何度呼んでも毎回env.flow_guideが含まれる（旧「__default__」共有キーは廃止）。"""
        result1 = collect_and_assemble(activity_id)
        result2 = collect_and_assemble(activity_id)

        assert "error" not in result1
        assert "flow_guide" in result1["env"]
        assert "error" not in result2
        assert "flow_guide" in result2["env"]


class TestCheckInTagNotes:
    """tag_notes注入の確認"""

    def test_tag_notes_injected(self, temp_db):
        """notesを持つタグがenv.tag_notesに含まれる"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "withnotes", "重要な教訓"),
            )
            conn.commit()
        finally:
            conn.close()

        activity = add_activity(
            title="Tag notes test",
            description="Desc",
            tags=["domain:withnotes"],
            check_in=False,
        )

        result = collect_and_assemble(activity["activity_id"])

        assert "error" not in result
        tag_notes = result["env"]["tag_notes"]
        assert len(tag_notes) == 1
        assert tag_notes[0]["tag"] == "domain:withnotes"
        assert tag_notes[0]["notes"] == "重要な教訓"

    def test_tag_notes_empty_when_no_notes(self, activity_id):
        """notesがないタグの場合、env.tag_notesキーは省略される"""
        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "tag_notes" not in result["env"]

    def test_intent_tag_notes_injected_every_time(self, temp_db):
        """intent:タグのnotesは毎回注入される（常時注入）"""
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE tags SET notes = ? WHERE namespace = 'intent' AND name = 'design'",
                ("設計の教訓",),
            )
            conn.commit()
        finally:
            conn.close()

        activity = add_activity(
            title="Design task",
            description="Desc",
            tags=["intent:design"],
            check_in=False,
        )
        aid = activity["activity_id"]

        # 1回目
        result1 = collect_and_assemble(aid)
        assert "error" not in result1
        intent_notes1 = [n for n in result1["env"]["tag_notes"] if n["tag"] == "intent:design"]
        assert len(intent_notes1) == 1

        # 2回目: intent: は常時注入なので再度返る
        result2 = collect_and_assemble(aid)
        assert "error" not in result2
        intent_notes2 = [n for n in result2["env"]["tag_notes"] if n["tag"] == "intent:design"]
        assert len(intent_notes2) == 1

    def test_non_intent_tag_notes_injected_once(self, temp_db):
        """intent:以外のタグのnotesは同一session_idでの初回のみ注入される"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "once", "1回だけの教訓"),
            )
            conn.commit()
        finally:
            conn.close()

        activity = add_activity(
            title="Domain task",
            description="Desc",
            tags=["domain:once"],
            check_in=False,
        )
        aid = activity["activity_id"]

        # 1回目: 注入される
        result1 = collect_and_assemble(aid, session_id="sess-1")
        assert "error" not in result1
        domain_notes1 = [n for n in result1["env"]["tag_notes"] if n["tag"] == "domain:once"]
        assert len(domain_notes1) == 1

        # 2回目（同じsession_id）: domain: は通常タグなので注入されない
        result2 = collect_and_assemble(aid, session_id="sess-1")
        assert "error" not in result2
        domain_notes2 = [n for n in result2["env"].get("tag_notes", []) if n["tag"] == "domain:once"]
        assert len(domain_notes2) == 0

    def test_no_session_id_never_dedups_tag_notes(self, temp_db):
        """session_id未解決（None）ではcheck_inのたび毎回notesが注入される
        （旧「__default__」共有キーは廃止。識別子が無いときは記録しない）。"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "unresolved", "識別子不明時の教訓"),
            )
            conn.commit()
        finally:
            conn.close()

        activity = add_activity(
            title="Unresolved session task",
            description="Desc",
            tags=["domain:unresolved"],
            check_in=False,
        )
        aid = activity["activity_id"]

        result1 = collect_and_assemble(aid, session_id=None)
        result2 = collect_and_assemble(aid, session_id=None)
        assert [n for n in result1["env"]["tag_notes"] if n["tag"] == "domain:unresolved"]
        assert [n for n in result2["env"]["tag_notes"] if n["tag"] == "domain:unresolved"]



class TestCheckInRelations:
    """リレーション関連のcheck-inテスト"""

    def test_related_activities_returned(self, temp_db):
        """関連アクティビティがcontext.activitiesに含まれる"""
        a1 = add_activity(title="親タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        a2 = add_activity(title="子タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a1["activity_id"], [{"type": "activity", "ids": [a2["activity_id"]]}])

        result = collect_and_assemble(a1["activity_id"])

        assert "error" not in result
        activities = result["context"]["activities"]
        assert len(activities) == 1
        assert activities[0]["id_raw"] == a2["activity_id"]
        assert activities[0]["title"] == "子タスク"

    def test_no_related_activities_key_when_empty(self, activity_id):
        """関連アクティビティがない場合、context.activitiesキーは省略される"""
        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "activities" not in result.get("context", {})

    def test_decisions_limited_to_max(self, temp_db):
        """decisionsがDECISIONS_FULL_LIMIT件に制限される"""
        topic = add_topic(title="決定多数トピック", description="Desc", tags=DEFAULT_TAGS)
        for i in range(DECISIONS_FULL_LIMIT + 5):
            add_decision(decision=f"決定事項{i}", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        assert len(result["context"]["decisions"]) == DECISIONS_FULL_LIMIT

    def test_related_topics_include_gravity_counts(self, temp_db):
        """context.topicsの各topicにdecisions_count/materials_countが含まれる"""
        topic = add_topic(title="重力テスト", description="Desc", tags=DEFAULT_TAGS)
        tid = topic["topic_id"]
        # decisionsを2件作る
        add_decision(decision="決定1", reason="理由", topic_id=tid)
        add_decision(decision="決定2", reason="理由", topic_id=tid)
        # materialを1件、直接紐づける
        add_material("資材1", "内容", DEFAULT_TAGS, "src", related=[{"type": "topic", "ids": [tid]}])
        # activity経由のmaterialはmaterials_countに含まれないことを確認するためのダミー
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [tid]}])
        add_material(
            "activity経由資材", "内容", DEFAULT_TAGS, "src",
            related=[{"type": "activity", "ids": [a["activity_id"]]}],
        )

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        topics = result["context"]["topics"]
        assert len(topics) == 1
        rt = topics[0]
        assert rt["id_raw"] == tid
        assert rt["decisions_count"] == 2
        # topic直接紐づけは1件のみ（activity経由のmaterialは含まない）
        assert rt["materials_count"] == 1

    def test_related_topics_zero_counts_present(self, temp_db):
        """decisions/materialsが無いtopicでもdecisions_count=0, materials_count=0が返る"""
        topic = add_topic(title="空のトピック", description="Desc", tags=DEFAULT_TAGS)
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        topics = result["context"]["topics"]
        assert len(topics) == 1
        rt = topics[0]
        assert rt["decisions_count"] == 0
        assert rt["materials_count"] == 0

    def test_related_topics_exclude_retracted_decisions(self, temp_db):
        """retracted decisionsはdecisions_countに含まれない"""
        topic = add_topic(title="retractテスト", description="Desc", tags=DEFAULT_TAGS)
        tid = topic["topic_id"]
        d1 = add_decision(decision="決定1", reason="理由", topic_id=tid)
        add_decision(decision="決定2", reason="理由", topic_id=tid)
        # 1件をretract
        retract_decision(d1["decision_id"])

        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [tid]}])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        rt = result["context"]["topics"][0]
        # retract済みを除いた1件のみカウント
        assert rt["decisions_count"] == 1

    def test_related_topics_multiple_topics_independent_counts(self, temp_db):
        """複数topicでそれぞれ独立したdecisions_count/materials_countが返る"""
        t1 = add_topic(title="トピック1", description="Desc", tags=DEFAULT_TAGS)
        t2 = add_topic(title="トピック2", description="Desc", tags=DEFAULT_TAGS)
        tid1, tid2 = t1["topic_id"], t2["topic_id"]
        add_decision(decision="d1a", reason="r", topic_id=tid1)
        add_decision(decision="d1b", reason="r", topic_id=tid1)
        add_decision(decision="d2a", reason="r", topic_id=tid2)
        add_material("m1", "c", DEFAULT_TAGS, "src", related=[{"type": "topic", "ids": [tid1]}])
        add_material("m2a", "c", DEFAULT_TAGS, "src", related=[{"type": "topic", "ids": [tid2]}])
        add_material("m2b", "c", DEFAULT_TAGS, "src", related=[{"type": "topic", "ids": [tid2]}])

        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [tid1, tid2]}])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        by_id = {rt["id_raw"]: rt for rt in result["context"]["topics"]}
        assert by_id[tid1]["decisions_count"] == 2
        assert by_id[tid1]["materials_count"] == 1
        assert by_id[tid2]["decisions_count"] == 1
        assert by_id[tid2]["materials_count"] == 2


class TestCheckInCoverage:
    """env.coverageフィールドのテスト"""

    def test_coverage_field_exists(self, activity_id):
        """env.coverageに必要な内訳キーが含まれる"""
        result = collect_and_assemble(activity_id)

        assert "error" not in result
        coverage = result["env"]["coverage"]
        assert "decisions" in coverage
        assert "materials" in coverage
        assert "logs" in coverage

    def test_coverage_no_relations_format(self, activity_id):
        """リレーションなしの場合、coverage分母は0"""
        result = collect_and_assemble(activity_id)

        assert "error" not in result
        coverage = result["env"]["coverage"]
        assert coverage["decisions"] == "0/0"
        assert coverage["materials"] == "0/0"
        assert coverage["logs"] == "0/0"

    def test_coverage_with_decisions(self, temp_db):
        """decisionsがある場合、coverageの分母に件数が反映される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        for i in range(3):
            add_decision(decision=f"決定{i}", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        # 分子: min(3, DECISIONS_FULL_LIMIT) = 3, 分母: 3
        assert result["env"]["coverage"]["decisions"] == "3/3"

    def test_coverage_decisions_exceeds_limit(self, temp_db):
        """decisions総数がDECISIONS_FULL_LIMITを超えた場合、分子は制限値になる"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        total = DECISIONS_FULL_LIMIT + 5
        for i in range(total):
            add_decision(decision=f"決定{i}", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        assert result["env"]["coverage"]["decisions"] == f"{DECISIONS_FULL_LIMIT}/{total}"

    def test_coverage_with_materials(self, activity_id):
        """materialsがある場合、coverageの分母に件数が反映される"""
        add_material("資材1", "内容1", DEFAULT_TAGS, "テスト用データ", related=[{"type": "activity", "ids": [activity_id]}])
        add_material("資材2", "内容2", DEFAULT_TAGS, "テスト用データ", related=[{"type": "activity", "ids": [activity_id]}])

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert result["env"]["coverage"]["materials"] == "2/2"

    def test_coverage_logs_includes_latest(self, temp_db):
        """logsの分子に最新ログ1件が加算される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        for i in range(3):
            add_log(topic_id=topic["topic_id"], title=f"ログ{i}", content=f"内容{i}")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        assert result["env"]["coverage"]["logs"] == "1/3"

    def test_coverage_zero_related_topics(self, activity_id):
        """関連するtopicが無い場合、coverage "0/0"が返る（Edge case）"""
        result = collect_and_assemble(activity_id)

        assert "error" not in result
        coverage = result["env"]["coverage"]
        assert coverage["decisions"] == "0/0"
        assert coverage["materials"] == "0/0"
        assert coverage["logs"] == "0/0"

    def test_coverage_not_affected_by_pinned_targets(self, temp_db):
        """pinsテーブル経由で注入されたpinned targetsはcoverageの分子に加算されない"""
        # activityに関連するtopic（coverageの分母・分子に計上される）
        related_topic = add_topic(title="関連トピック", description="Desc", tags=DEFAULT_TAGS)
        add_decision(decision="通常の決定", reason="理由", topic_id=related_topic["topic_id"])
        # activityに関連しないtopic（coverage対象外）にdecisionを作成
        unrelated_topic = add_topic(title="無関係トピック", description="Desc", tags=DEFAULT_TAGS)
        unrelated_d = add_decision(decision="pin用決定", reason="理由", topic_id=unrelated_topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [related_topic["topic_id"]]}])
        # 無関係topicのdecisionをpin → pins注入されるがcoverageには含まれないはず
        add_pin("activity", a["activity_id"], "decision", unrelated_d["decision_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        assert len(result["anchor"]["pinned"]["decisions"]) == 1
        # coverageは関連topic配下のdecisionのみ: 通常1件/全体1件。pin注入分は加算されない
        assert result["env"]["coverage"]["decisions"] == "1/1"


class TestCheckInLogsCatalog:
    """logsカタログのテスト"""

    def test_logs_empty_without_relations(self, activity_id):
        """リレーションなしの場合、context.latest_log/catalog.logsともキーが省略される"""
        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "latest_log" not in result.get("context", {})
        assert "logs" not in result.get("catalog", {})

    def test_latest_log_has_content(self, temp_db):
        """最新ログ1件がcontent付きでcontext.latest_logに返る"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        add_log(topic_id=topic["topic_id"], title="初回議論", content="詳細な内容")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        latest_log = result["context"]["latest_log"]
        assert latest_log["title"] == "初回議論"
        assert latest_log["content"] == "詳細な内容"
        assert "logs" not in result.get("catalog", {})

    def test_logs_catalog_excludes_latest(self, temp_db):
        """最新1件以外のlogsはid+titleのカタログとして返る"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        add_log(topic_id=topic["topic_id"], title="古いログ", content="古い内容")
        add_log(topic_id=topic["topic_id"], title="新しいログ", content="新しい内容")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        assert result["context"]["latest_log"]["title"] == "新しいログ"
        assert result["context"]["latest_log"]["content"] == "新しい内容"
        logs = result["catalog"]["logs"]
        assert len(logs) == 1
        assert logs[0]["title"] == "古いログ"
        assert "content" not in logs[0]

    def test_logs_catalog_multiple_topics(self, temp_db):
        """複数topicのlogsが集約される（最新1件がlatest_log、残りがカタログ）"""
        t1 = add_topic(title="トピック1", description="Desc", tags=DEFAULT_TAGS)
        t2 = add_topic(title="トピック2", description="Desc", tags=DEFAULT_TAGS)
        add_log(topic_id=t1["topic_id"], title="ログA", content="内容A")
        add_log(topic_id=t2["topic_id"], title="ログB", content="内容B")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [t1["topic_id"], t2["topic_id"]]}])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        assert "latest_log" in result["context"]
        logs = result["catalog"]["logs"]
        assert len(logs) == 1
        all_titles = {result["context"]["latest_log"]["title"]} | {l["title"] for l in logs}
        assert "ログA" in all_titles
        assert "ログB" in all_titles


class TestCheckInDependencies:
    """check-in結果のcontrol.dependenciesフィールドのテスト"""

    def test_dependencies_present_when_depends_on_exists(self, temp_db):
        """depends_on関係がある場合、control.dependenciesフィールドが結果に含まれる"""
        dep = add_activity(title="依存先タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        main = add_activity(title="メインタスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (main["activity_id"], dep["activity_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        result = collect_and_assemble(main["activity_id"])

        assert "error" not in result
        deps = result["control"]["dependencies"]
        assert len(deps) == 1
        assert deps[0]["id_raw"] == dep["activity_id"]
        assert deps[0]["title"] == "依存先タスク"
        assert deps[0]["status"] == "pending"

    def test_dependencies_absent_when_no_depends_on(self, activity_id):
        """depends_on関係がない場合、control.dependenciesフィールドは省略される"""
        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "dependencies" not in result["control"]

    def test_dependencies_multiple(self, temp_db):
        """複数の依存先がある場合、全件がdependenciesに含まれる"""
        dep1 = add_activity(title="依存先1", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        dep2 = add_activity(title="依存先2", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        main = add_activity(title="メインタスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (main["activity_id"], dep1["activity_id"]),
            )
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (main["activity_id"], dep2["activity_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        result = collect_and_assemble(main["activity_id"])

        assert "error" not in result
        deps = result["control"]["dependencies"]
        assert len(deps) == 2
        dep_ids = {d["id_raw"] for d in deps}
        assert dep1["activity_id"] in dep_ids
        assert dep2["activity_id"] in dep_ids

    def test_dependencies_includes_completed(self, temp_db):
        """completedの依存先もdependenciesに含まれる（状態情報として有用）"""
        dep = add_activity(title="完了済み依存先", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(dep["activity_id"], status="completed")
        main = add_activity(title="メインタスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (main["activity_id"], dep["activity_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        result = collect_and_assemble(main["activity_id"])

        assert "error" not in result
        assert result["control"]["dependencies"][0]["status"] == "completed"

    def test_dependencies_status_reflects_current(self, temp_db):
        """dependenciesの各要素のstatusがDB上の最新値を反映する"""
        dep = add_activity(title="進行中タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        update_activity(dep["activity_id"], status="in_progress")
        main = add_activity(title="メインタスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (main["activity_id"], dep["activity_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        result = collect_and_assemble(main["activity_id"])

        assert "error" not in result
        assert result["control"]["dependencies"][0]["status"] == "in_progress"


class TestCheckInPinned:
    """pinsテーブル経由のpinned target注入テスト"""

    def test_no_pinned_field_when_nothing_pinned(self, temp_db):
        """pinsテーブルに対象activity向けのpinがない場合、anchor.pinnedフィールドは省略される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        add_decision(decision="通常の決定", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        assert "pinned" not in result["anchor"]

    def test_activity_source_pin_injects_decision(self, temp_db):
        """source=activityのpinsテーブルエントリが、check-in時にpinned.decisionsにcontent付きで注入される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d = add_decision(decision="重要な決定", reason="根本的な理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        # pinsにsource=activityでdecisionをpin
        add_pin("activity", a["activity_id"], "decision", d["decision_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        pinned = result["anchor"]["pinned"]
        assert len(pinned["decisions"]) == 1
        assert pinned["decisions"][0]["title"] == "重要な決定"
        assert pinned["decisions"][0]["reason"] == "根本的な理由"

    def test_tag_source_pin_injects_decision(self, temp_db):
        """source=tag（activity自身のtag）のpinsテーブルエントリが、check-in時にpinned.decisionsに注入される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d = add_decision(decision="タグ経由重要決定", reason="根拠", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        # pinsにsource=tagでdecisionをpin（domain:testタグ）
        add_pin("tag", "domain:test", "decision", d["decision_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        pinned = result["anchor"]["pinned"]
        assert len(pinned["decisions"]) == 1
        assert pinned["decisions"][0]["title"] == "タグ経由重要決定"

    def test_pinned_decisions_included_in_recent_decisions(self, temp_db):
        """pinsテーブルでpinされたdecisionはcontext.decisionsにも通常通り含まれる（除外されない）"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d = add_decision(decision="ピン済み決定", reason="理由", topic_id=topic["topic_id"])
        add_decision(decision="通常の決定", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])
        # decisionをpinする
        add_pin("activity", a["activity_id"], "decision", d["decision_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        # context.decisionsにはピン済み・非ピンの両方が含まれる（pinned列による除外なし）
        decisions = result["context"]["decisions"]
        assert len(decisions) == 2
        titles = {dec["title"] for dec in decisions}
        assert "ピン済み決定" in titles
        assert "通常の決定" in titles

    def test_activity_source_pin_injects_log(self, temp_db):
        """source=activityのpinsテーブルエントリが、check-in時にpinned.logsにcontent付きで注入される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        log = add_log(topic_id=topic["topic_id"], title="方向転換ログ", content="## 経緯\n重要な方向転換")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_pin("activity", a["activity_id"], "log", log["log_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        pinned = result["anchor"]["pinned"]
        assert len(pinned["logs"]) == 1
        assert pinned["logs"][0]["title"] == "方向転換ログ"
        assert pinned["logs"][0]["content"] == "## 経緯\n重要な方向転換"

    def test_pinned_log_also_appears_in_latest_log(self, temp_db):
        """pinsテーブルでpinされたlogはcontext.latest_logにも通常通り含まれる（除外されない）"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        log1 = add_log(topic_id=topic["topic_id"], title="ピン済みログ", content="内容1")
        add_log(topic_id=topic["topic_id"], title="新しいログ", content="内容2")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])
        # log1をpinするが、IDが小さい（古い）ため latest_log には新しい方が来る
        add_pin("activity", a["activity_id"], "log", log1["log_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        # latest_logには最新のログが入る（pinned列による除外なし）
        assert result["context"]["latest_log"]["title"] == "新しいログ"
        # logsカタログにはpinされたログが残る
        logs = result["catalog"]["logs"]
        assert len(logs) == 1
        assert logs[0]["title"] == "ピン済みログ"

    def test_activity_source_pin_injects_material(self, temp_db):
        """source=activityのpinsテーブルエントリが、check-in時にpinned.materialsにcontent付きで注入される"""
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        m = add_material("設計書", "# 設計\n詳細な内容", DEFAULT_TAGS, "テスト用データ",
                         related=[{"type": "activity", "ids": [a["activity_id"]]}])
        add_pin("activity", a["activity_id"], "material", m["material_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        pinned = result["anchor"]["pinned"]
        assert len(pinned["materials"]) == 1
        assert pinned["materials"][0]["title"] == "設計書"
        assert pinned["materials"][0]["content"] == "# 設計\n詳細な内容"
        assert pinned["materials"][0]["source"] == "テスト用データ"

    def test_pinned_material_also_appears_in_materials(self, temp_db):
        """pinsテーブルでpinされたmaterialはcontext.materialsにも通常通り含まれる（除外されない）"""
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        m1 = add_material("ピン資材", "内容1", DEFAULT_TAGS, "テスト用データ",
                          related=[{"type": "activity", "ids": [a["activity_id"]]}])
        add_material("通常資材", "内容2", DEFAULT_TAGS, "テスト用データ",
                     related=[{"type": "activity", "ids": [a["activity_id"]]}])
        add_pin("activity", a["activity_id"], "material", m1["material_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        # context.materialsにはpin済みも非ピンも両方含まれる（pinned列による除外なし）
        materials = result["context"]["materials"]
        assert len(materials) == 2
        titles = {m["title"] for m in materials}
        assert "ピン資材" in titles
        assert "通常資材" in titles

    def test_activity_source_pin_injects_topic(self, temp_db):
        """source=activityのpinsテーブルエントリが、check-in時にpinned.topicsに注入される"""
        topic = add_topic(title="重要トピック", description="Desc", tags=DEFAULT_TAGS)
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_pin("activity", a["activity_id"], "topic", topic["topic_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        pinned = result["anchor"]["pinned"]
        assert len(pinned["topics"]) == 1
        assert pinned["topics"][0]["id_raw"] == topic["topic_id"]
        assert pinned["topics"][0]["title"] == "重要トピック"

    def test_activity_source_pin_injects_activity(self, temp_db):
        """source=activityのpinsテーブルエントリが、check-in時にpinned.activitiesに注入される"""
        a1 = add_activity(title="メインタスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        a2 = add_activity(title="重要参照タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_pin("activity", a1["activity_id"], "activity", a2["activity_id"])

        result = collect_and_assemble(a1["activity_id"])

        assert "error" not in result
        pinned = result["anchor"]["pinned"]
        assert len(pinned["activities"]) == 1
        assert pinned["activities"][0]["id_raw"] == a2["activity_id"]
        assert pinned["activities"][0]["title"] == "重要参照タスク"
        assert pinned["activities"][0]["status"] == "pending"

    def test_distinct_deduplication_when_multiple_routes(self, temp_db):
        """同一targetがtagソースとactivityソースの両方からpinされても、pinned結果に1件だけ注入される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d = add_decision(decision="重複テスト決定", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        # activityソースとtagソースの両方からdecisionをpin
        add_pin("activity", a["activity_id"], "decision", d["decision_id"])
        add_pin("tag", "domain:test", "decision", d["decision_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        pinned = result["anchor"]["pinned"]
        # (target_type, target_id) でDISTINCTされ、1件のみ
        assert len(pinned["decisions"]) == 1
        assert pinned["decisions"][0]["title"] == "重複テスト決定"

    def test_retracted_decision_excluded_from_pinned(self, temp_db):
        """retractされたdecisionはpinsテーブル経由でもpinned.decisionsに注入されない"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d = add_decision(decision="取り消し済み決定", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_pin("activity", a["activity_id"], "decision", d["decision_id"])
        # decisionをretract
        retract_decision(d["decision_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        # retractされているためpinned.decisionsキー自体が省略される
        pinned = result["anchor"].get("pinned", {})
        assert "decisions" not in pinned

    def test_tag_source_only_uses_activity_own_tags(self, temp_db):
        """tagソースのpinは、check-in対象activityが持つtagのみが使用される（他activityのtagは無視される）"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d = add_decision(decision="他タグ経由決定", reason="理由", topic_id=topic["topic_id"])
        a1 = add_activity(title="メインタスク", description="Desc", tags=["domain:test"], check_in=False)
        # a1が持たないタグ（domain:other）をsourceとしてdecisionをpin
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name) VALUES ('domain', 'other')",
            )
            other_tag_row = conn.execute(
                "SELECT id FROM tags WHERE namespace='domain' AND name='other'"
            ).fetchone()
            conn.execute(
                "INSERT INTO pins (source_type, source_id, target_type, target_id) VALUES ('tag', ?, 'decision', ?)",
                (other_tag_row["id"], d["decision_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        result = collect_and_assemble(a1["activity_id"])

        assert "error" not in result
        # a1はdomain:otherタグを持たないため、そのpinは注入されない
        assert "pinned" not in result["anchor"]

    def test_all_five_target_types_in_pinned(self, temp_db):
        """decision/log/material/topic/activityの5種すべてがanchor.pinnedフィールドに含まれる"""
        topic = add_topic(title="重要トピック", description="Desc", tags=DEFAULT_TAGS)
        d = add_decision(decision="重要決定", reason="理由", topic_id=topic["topic_id"])
        log = add_log(topic_id=topic["topic_id"], title="重要ログ", content="内容")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        m = add_material("重要資材", "内容", DEFAULT_TAGS, "テスト用データ",
                         related=[{"type": "activity", "ids": [a["activity_id"]]}])
        a2 = add_activity(title="参照タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)

        add_pin("activity", a["activity_id"], "decision", d["decision_id"])
        add_pin("activity", a["activity_id"], "log", log["log_id"])
        add_pin("activity", a["activity_id"], "material", m["material_id"])
        add_pin("activity", a["activity_id"], "topic", topic["topic_id"])
        add_pin("activity", a["activity_id"], "activity", a2["activity_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        pinned = result["anchor"]["pinned"]
        assert len(pinned["decisions"]) == 1
        assert len(pinned["logs"]) == 1
        assert len(pinned["materials"]) == 1
        assert len(pinned["topics"]) == 1
        assert len(pinned["activities"]) == 1

    def test_zero_key_omission_in_pinned(self, temp_db):
        """pinned結果で0件のキーは省略される"""
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        # topicのみをpin（他のtypeはピンなし）
        add_pin("activity", a["activity_id"], "topic", topic["topic_id"])

        result = collect_and_assemble(a["activity_id"])

        assert "error" not in result
        pinned = result["anchor"]["pinned"]
        assert "topics" in pinned
        # 0件のキーは省略される
        assert "decisions" not in pinned
        assert "logs" not in pinned
        assert "materials" not in pinned
        assert "activities" not in pinned


# recomposeナッジhintの境界条件テスト用。domain: namespaceのみが対象。
DOMAIN_TAG_NAME = "hint-target"
DOMAIN_TAG = f"domain:{DOMAIN_TAG_NAME}"
PLAIN_TAG = "recompose-target"  # 素タグはhint対象外


def _set_material_updated_at(material_id: int, ts: str) -> None:
    """materialのupdated_atを指定文字列に上書きする（基準時刻Tの制御用）。

    add_material/update_materialはupdated_atを現在時刻でセットするため、
    decisionとの前後関係を秒未満の精度に依存させずテストするには、
    updated_atを固定値に直接書き換える必要がある。
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE materials SET updated_at = ? WHERE id = ?",
            (ts, material_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_decision_created_at(decision_id: int, ts: str) -> None:
    """decisionのcreated_atを指定文字列に上書きする（基準時刻Tとの前後制御用）。"""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE decisions SET created_at = ? WHERE id = ?",
            (ts, decision_id),
        )
        conn.commit()
    finally:
        conn.close()


def _get_tag_notes(name: str, namespace: str = "domain") -> str:
    """指定タグのnotesを別connで読み出す（commit有無の検証用）。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT notes FROM tags WHERE namespace = ? AND name = ?",
            (namespace, name),
        ).fetchone()
        return row["notes"] or "" if row else ""
    finally:
        conn.close()


def _make_activity_with_domain_tag() -> int:
    """domain:タグ DOMAIN_TAG を持つアクティビティを作成しIDを返す。

    intent:implement を含むため IMPLEMENT_WORKFLOW_GUARD 用に
    dummy decision を作って related に含める。
    """
    topic = add_topic(title="dummy topic", description="d", tags=["domain:dummy"])
    dec = add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
    result = add_activity(
        title="[作業] recompose対象タスク",
        description="recomposeナッジ判定の対象タスク",
        tags=[DOMAIN_TAG, "intent:implement"],
        related=[{"type": "decision", "ids": [dec["decision_id"]]}],
        check_in=False,
    )
    return result["activity_id"]


def _make_topic_with_domain_tag(title: str = "recomposeトピック") -> int:
    """domain:タグ DOMAIN_TAG を持つトピックを作成しIDを返す（topic_tags継承経路用）。"""
    result = add_topic(title=title, description="Desc", tags=[DOMAIN_TAG])
    return result["topic_id"]


class TestRecomposeHints:
    """check_in結果のrecomposeナッジhint生成の統合テスト"""

    def test_bootstrap_hint_fires_at_threshold_via_topic_tags(self, temp_db):
        """material未pinのtagで、topic_tags継承のdecisionがブートストラップしきい値ちょうど蓄積するとhintが発火する"""
        activity_id = _make_activity_with_domain_tag()
        topic_id = _make_topic_with_domain_tag()
        for i in range(_RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"決定{i}", reason="理由", topic_id=topic_id)

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        hints = result["env"]["hints"]
        bootstrap_hints = [h for h in hints if "蓄積しています" in h]
        assert len(bootstrap_hints) == 1
        assert DOMAIN_TAG in bootstrap_hints[0]
        assert str(_RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD) in bootstrap_hints[0]

    def test_bootstrap_hint_absent_below_threshold(self, temp_db):
        """material未pinのtagで、decisionがブートストラップしきい値-1件のときhintは発火せずキーも無い"""
        activity_id = _make_activity_with_domain_tag()
        topic_id = _make_topic_with_domain_tag()
        for i in range(_RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD - 1):
            add_decision(decision=f"決定{i}", reason="理由", topic_id=topic_id)

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "hints" not in result["env"]

    def test_bootstrap_hint_fires_via_decision_tags_direct(self, temp_db):
        """material未pinのtagで、decision_tags直付けのdecisionがしきい値蓄積するとブートストラップhintが発火する"""
        activity_id = _make_activity_with_domain_tag()
        # topicにはdomain:hint-target を付けず、decision側に直接付ける
        topic = add_topic(title="無タグトピック", description="Desc", tags=["domain:other"])
        topic_id = topic["topic_id"]
        for i in range(_RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD):
            add_decision(
                decision=f"決定{i}", reason="理由", topic_id=topic_id,
                tags=["domain:other", DOMAIN_TAG],
            )

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert any("蓄積しています" in h and DOMAIN_TAG in h for h in result["env"]["hints"])

    def test_bootstrap_hint_excludes_retracted_decisions(self, temp_db):
        """retractedなdecisionはブートストラップ判定の件数に含まれない"""
        activity_id = _make_activity_with_domain_tag()
        topic_id = _make_topic_with_domain_tag()
        # しきい値ちょうど作成し、うち1件をretractすると しきい値-1 になり発火しない
        decision_ids = []
        for i in range(_RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD):
            d = add_decision(decision=f"決定{i}", reason="理由", topic_id=topic_id)
            decision_ids.append(d["decision_id"])
        retract_decision(decision_ids[0])

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "hints" not in result["env"]

    def test_delta_hint_fires_at_threshold(self, temp_db):
        """material pin済みのtagで、material最終更新後のdecisionが増分しきい値ちょうど増えるとメンテhintが発火する"""
        activity_id = _make_activity_with_domain_tag()
        topic_id = _make_topic_with_domain_tag()

        # 基準時刻Tより前のdecision（増分にカウントされない）
        old_decision = add_decision(decision="旧決定", reason="理由", topic_id=topic_id)
        _set_decision_created_at(old_decision["decision_id"], "2024-01-01 00:00:00")

        # tagにmaterialをpinし、updated_at（基準時刻T）を固定
        mat = add_material(
            title="統合material", content="まとめ", tags=["domain:test", DOMAIN_TAG],
            source="recompose",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        # 基準時刻Tより後のdecisionをしきい値ちょうど作成
        for i in range(_RECOMPOSE_HINT_DELTA_THRESHOLD):
            d = add_decision(decision=f"新決定{i}", reason="理由", topic_id=topic_id)
            _set_decision_created_at(d["decision_id"], "2024-07-01 00:00:00")

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        hints = result["env"]["hints"]
        delta_hints = [h for h in hints if "最終更新以降" in h]
        assert len(delta_hints) == 1
        assert DOMAIN_TAG in delta_hints[0]
        assert str(_RECOMPOSE_HINT_DELTA_THRESHOLD) in delta_hints[0]

    def test_delta_hint_absent_below_threshold(self, temp_db):
        """material pin済みのtagで、最終更新後のdecisionが増分しきい値-1件のときメンテhintは発火しない"""
        activity_id = _make_activity_with_domain_tag()
        topic_id = _make_topic_with_domain_tag()

        mat = add_material(
            title="統合material", content="まとめ", tags=["domain:test", DOMAIN_TAG],
            source="recompose",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        for i in range(_RECOMPOSE_HINT_DELTA_THRESHOLD - 1):
            d = add_decision(decision=f"新決定{i}", reason="理由", topic_id=topic_id)
            _set_decision_created_at(d["decision_id"], "2024-07-01 00:00:00")

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "hints" not in result["env"]

    def test_delta_hint_excludes_decisions_before_base_time(self, temp_db):
        """material最終更新時刻T以前のdecisionは増分カウントに含まれない"""
        activity_id = _make_activity_with_domain_tag()
        topic_id = _make_topic_with_domain_tag()

        mat = add_material(
            title="統合material", content="まとめ", tags=["domain:test", DOMAIN_TAG],
            source="recompose",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        # しきい値件数だけ作るが、すべてT以前なので増分0となり発火しない
        for i in range(_RECOMPOSE_HINT_DELTA_THRESHOLD):
            d = add_decision(decision=f"旧決定{i}", reason="理由", topic_id=topic_id)
            _set_decision_created_at(d["decision_id"], "2024-05-01 00:00:00")

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "hints" not in result["env"]

    def test_delta_hint_uses_max_updated_at_across_pinned_materials(self, temp_db):
        """tagに複数materialがpinされている場合、基準時刻Tは最大のupdated_atになる"""
        activity_id = _make_activity_with_domain_tag()
        topic_id = _make_topic_with_domain_tag()

        mat_old = add_material(
            title="古い統合", content="まとめ", tags=["domain:test", DOMAIN_TAG], source="recompose",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat_old["material_id"])
        _set_material_updated_at(mat_old["material_id"], "2024-01-01 00:00:00")

        mat_new = add_material(
            title="新しい統合", content="まとめ", tags=["domain:test", DOMAIN_TAG], source="recompose",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat_new["material_id"])
        _set_material_updated_at(mat_new["material_id"], "2024-06-01 00:00:00")

        # T=2024-06-01（max）と2024-01-01の間に置いたdecisionは増分に含まれない。
        # しきい値件数をこの区間に置くと、maxを基準とするため発火しない。
        for i in range(_RECOMPOSE_HINT_DELTA_THRESHOLD):
            d = add_decision(decision=f"中間決定{i}", reason="理由", topic_id=topic_id)
            _set_decision_created_at(d["decision_id"], "2024-03-01 00:00:00")

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "hints" not in result["env"], (
            "基準時刻Tが最大のupdated_at（2024-06-01）でなく最小（2024-01-01）で評価されている"
        )

    def test_plain_tag_excluded_from_hints(self, temp_db):
        """素タグはhint判定対象外で、domain:タグがなければhintは出ない"""
        # 素タグのみを持つアクティビティ。intent:implement は IMPLEMENT_WORKFLOW_GUARD 用
        plain_topic = add_topic(
            title="dummy", description="d", tags=["domain:dummy"],
        )
        plain_dec = add_decision(
            decision="d", reason="r", topic_id=plain_topic["topic_id"],
        )
        result_a = add_activity(
            title="[作業] 素タグのみ",
            description="domain:タグなし",
            tags=[PLAIN_TAG, "intent:implement"],
            related=[{"type": "decision", "ids": [plain_dec["decision_id"]]}],
            check_in=False,
        )
        activity_id = result_a["activity_id"]
        # 素タグを付けたtopicにブートストラップしきい値を超えるdecisionを蓄積
        topic = add_topic(
            title="素タグトピック", description="Desc", tags=[PLAIN_TAG],
        )
        for i in range(_RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD + 5):
            add_decision(decision=f"決定{i}", reason="理由", topic_id=topic["topic_id"])

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "hints" not in result["env"], (
            "素タグ（namespace空文字）がhint判定対象になっている"
        )

    def test_hints_key_absent_when_no_tag_fires(self, temp_db):
        """どのtagも発火条件を満たさないとき、env.hintsキーは含まれない"""
        activity_id = _make_activity_with_domain_tag()
        topic_id = _make_topic_with_domain_tag()
        add_decision(decision="単一決定", reason="理由", topic_id=topic_id)

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "hints" not in result["env"]


class TestRecomposeCooldownTransaction:
    """hint取得後にcheck_in本体が失敗した場合のトランザクション境界のテスト"""

    def test_cooldown_marker_not_committed_when_check_in_fails_after_hint(
        self, temp_db, monkeypatch
    ):
        """_get_immediate_hints呼び出し後、check_in本体が例外で失敗した場合、
        hintは応答されずDATABASE_ERRORになる一方、クールダウンマーカーの書き込みも
        ロールバックされ、notesには残らないこと（hint未達のまま消費されない）"""
        activity_id = _make_activity_with_domain_tag()
        topic_id = _make_topic_with_domain_tag()
        for i in range(_RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"決定{i}", reason="理由", topic_id=topic_id)

        def _boom(*args, **kwargs):
            raise RuntimeError("boom after hint generation")

        monkeypatch.setattr(checkin_tier_service, "_cap_asks", _boom)

        result = collect_and_assemble(activity_id)

        assert result.get("error", {}).get("code") == "DATABASE_ERROR"
        assert MARKER_RECOMPOSE_BOOTSTRAP not in _get_tag_notes(DOMAIN_TAG_NAME)

        # マーカーがロールバックされているため、パッチを戻して再度check_inすれば
        # hintが再発火する
        monkeypatch.undo()
        result_retry = collect_and_assemble(activity_id)
        assert "error" not in result_retry
        assert any("蓄積しています" in h for h in result_retry["env"]["hints"])


# activity_cleanup hintの本番到達経路(check_in経由)テスト用。
ACTIVITY_MANAGEMENT_TAG_NAME = "activity-management"


def _ensure_activity_management_tag() -> None:
    """activity-managementタグ(namespace無しの素タグ)をtags行として存在させる。"""
    add_topic(title="am-anchor-checkin", description="d", tags=[ACTIVITY_MANAGEMENT_TAG_NAME])


def _make_stale_activity_for_checkin() -> int:
    """activity_cleanupの母集団に入る放置activityを作る(status=pending・
    updated_atを2000年に固定)。"""
    result = add_activity(
        title="[作業] 放置対象", description="d",
        tags=["domain:activity-cleanup-target"],
        check_in=False,
    )
    activity_id = result["activity_id"]
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE activities SET status = 'pending', updated_at = '2000-01-01 00:00:00' "
            "WHERE id = ?",
            (activity_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return activity_id


class TestActivityCleanupHintViaCheckIn:
    """activity_cleanup hintが本番の到達経路(checkin_tier_service.collect_and_assemble経由、
    マーカー永続化はcheck_in末尾のcommitに依存)で正しく動作することの統合テスト。

    tests/unit/test_hint_service.pyの同種テストはget_hints(自前connでcommitする
    公開API)経由で書かれており、check_in→get_hints_with_conn(commitしない)→
    check_in末尾commit、という本番の経路を通していない。"""

    def test_same_day_refire_suppressed_via_check_in_commit_path(self, temp_db):
        """check_in経由の1回目でactivity_cleanup hintが発火し、そのクールダウン
        マーカー書き込みがcheck_in末尾のcommitで実際に永続化されることで、
        2回目のcheck_inでは同日中は抑制されることを確認する。

        check_in対象自身はstatus自動更新でupdated_atが現在時刻に更新されるため、
        放置母集団に含めると自分自身の判定への寄与を消費してしまう。そのため
        check_in対象(actor_id)は放置母集団とは別に用意する"""
        _ensure_activity_management_tag()
        for _ in range(ACTIVITY_CLEANUP_COUNT_THRESHOLD):
            _make_stale_activity_for_checkin()
        actor_id = add_activity(
            title="[作業] check_in主体", description="d",
            tags=["domain:checkin-actor"],
            check_in=False,
        )["activity_id"]

        result_first = collect_and_assemble(actor_id)
        assert "error" not in result_first
        assert any(
            ACTIVITY_CLEANUP_AUTOTRIGGER_GUARD in h
            for h in result_first["env"].get("hints", [])
        )
        assert MARKER_ACTIVITY_CLEANUP in _get_tag_notes(
            ACTIVITY_MANAGEMENT_TAG_NAME, namespace=""
        )

        result_second = collect_and_assemble(actor_id)
        assert "error" not in result_second
        assert not any(
            ACTIVITY_CLEANUP_AUTOTRIGGER_GUARD in h
            for h in result_second["env"].get("hints", [])
        )


class TestCheckInSessionRegistry:
    """check_inのセッション別名レジストリ統合（env.session、非致命性、衝突検出）。"""

    @pytest.fixture(autouse=True)
    def _isolate_registry_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            session_registry_service.REGISTRY_PATH_ENV,
            str(tmp_path / "session_aliases.json"),
        )

    def _stub_world(self, monkeypatch, sessions: dict[str, dict]):
        """sessions: {bridge_session_id: {"cli_pid", "cli_session_id", "name"}}"""

        def resolve(bridge_session_id):
            info = sessions.get(bridge_session_id)
            return dict(info, cwd=None, cli_status=None) if info else None

        def is_alive(pid):
            return any(info["cli_pid"] == pid for info in sessions.values())

        def read_cli(pid):
            for info in sessions.values():
                if info["cli_pid"] == pid:
                    return dict(info, cwd=None, cli_status=None)
            return None

        monkeypatch.setattr(session_identity, "resolve_cli_session", resolve)
        monkeypatch.setattr(session_registry_service, "is_process_alive", is_alive)
        monkeypatch.setattr(session_registry_service.cli_session, "read_cli_session", read_cli)

    def test_session_field_populated_when_cli_resolved(self, activity_id, monkeypatch):
        self._stub_world(
            monkeypatch,
            {"bridge-1": {"cli_pid": 100, "cli_session_id": "cli-1", "name": "workspace-a1"}},
        )
        monkeypatch.setattr(session_identity, "get_caller_session_id", lambda: "bridge-1")

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert result["env"]["session"] == {
            "name": "workspace-a1",
            "alias": "[作業] タグnotesカラム追加",
            "alias_collision": False,
        }

    def test_session_field_reports_unresolved_when_bridge_id_missing(
        self, activity_id, monkeypatch
    ):
        """呼び出し元のbridge session idが取れない場合、check_in本体は正常応答する"""
        monkeypatch.setattr(session_identity, "get_caller_session_id", lambda: None)

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert result["env"]["session"] == {"registered": False, "reason": "cli_unresolved"}
        assert "activity" in result["anchor"]

    def test_registry_exception_does_not_fail_check_in(self, activity_id, monkeypatch, tmp_path):
        """レジストリ更新側が例外を投げても、check_in本体は成功応答を返す"""
        self._stub_world(
            monkeypatch,
            {"bridge-1": {"cli_pid": 100, "cli_session_id": "cli-1", "name": "workspace-a1"}},
        )
        monkeypatch.setattr(session_identity, "get_caller_session_id", lambda: "bridge-1")

        # レジストリファイルの親をファイルで塞ぎ、flock/書き込み時にOSErrorを
        # 自然発生させる（内部関数の直接mockを避け、外部境界であるファイルI/O
        # 側から例外を誘発する）
        blocked_parent = tmp_path / "blocked"
        blocked_parent.write_text("not a directory")
        monkeypatch.setenv(
            session_registry_service.REGISTRY_PATH_ENV,
            str(blocked_parent / "session_aliases.json"),
        )

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert result["env"]["session"] == {"registered": False, "reason": "cli_unresolved"}
        assert "activity" in result["anchor"]

    def test_collision_marks_alias_collision_in_session(self, activity_id, monkeypatch):
        """別セッションが同じ導出aliasを既に確保している場合、env.sessionに衝突が記録される"""
        self._stub_world(
            monkeypatch,
            {
                "bridge-other": {"cli_pid": 200, "cli_session_id": "cli-2", "name": "workspace-b1"},
                "bridge-1": {"cli_pid": 100, "cli_session_id": "cli-1", "name": "workspace-a1"},
            },
        )
        session_registry_service.register_checkin(
            bridge_session_id="bridge-other",
            activity_id=999,
            activity_title="[作業] タグnotesカラム追加",
            activity_status="in_progress",
        )
        monkeypatch.setattr(session_identity, "get_caller_session_id", lambda: "bridge-1")

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert result["env"]["session"]["alias_collision"] is True
        assert result["env"]["session"]["alias"] == "[作業] タグnotesカラム追加-2"

    def test_add_activity_check_in_true_also_registers_session(self, temp_db, monkeypatch):
        """add_activity(check_in=True)経由でもレジストリ行が作られる
        （内部でcheckin_tier_service.collect_and_assembleを呼ぶ経路のカバレッジ）"""
        self._stub_world(
            monkeypatch,
            {"bridge-1": {"cli_pid": 100, "cli_session_id": "cli-1", "name": "workspace-a1"}},
        )
        monkeypatch.setattr(session_identity, "get_caller_session_id", lambda: "bridge-1")

        result = add_activity(
            title="[作業] 新規タスク",
            description="説明",
            tags=DEFAULT_TAGS,
            check_in=True,
        )

        assert "error" not in result
        registered = session_registry_service.list_sessions(self_bridge_session_id="bridge-1")
        assert len(registered) == 1
        assert registered[0]["activity_title"] == "[作業] 新規タスク"
        assert registered[0]["is_self"] is True


class TestCheckInSessionLedger:
    """check_inからsession_ledger_service.record_checkinへの配線の統合テスト。"""

    def _fetch_session(self, session_id: str):
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def test_records_last_checkin_on_existing_ledger_row(self, activity_id, monkeypatch):
        session_ledger_service.register(
            "bridge-1", id_kind="bridge", harness=None, host="h", mode="interactive",
        )
        monkeypatch.setattr(session_identity, "get_caller_session_id", lambda: "bridge-1")

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        row = self._fetch_session("bridge-1")
        assert row["last_checkin_activity_id"] == activity_id
        assert row["last_checkin_at"] is not None

    def test_no_bridge_id_does_not_fail_check_in(self, activity_id, monkeypatch):
        monkeypatch.setattr(session_identity, "get_caller_session_id", lambda: None)

        result = collect_and_assemble(activity_id)

        assert "error" not in result

    def test_ledger_write_exception_does_not_fail_check_in(self, activity_id, monkeypatch):
        def boom(session_id, activity_id):
            raise RuntimeError("boom")

        monkeypatch.setattr(session_identity, "get_caller_session_id", lambda: "bridge-1")
        monkeypatch.setattr(session_ledger_service, "record_checkin", boom)

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert "activity" in result["anchor"]


class TestCheckInGoalBlock:
    """checkin_tier_service.collect_and_assembleのgoalブロック配線の統合テスト。

    ラベル・次の一手の導出ロジック自体はtest_goal_service_derive.pyが担保するため、
    ここではcollect_and_assembleがgoal_serviceを正しく呼び出し、活配置・例外処理・
    再オープンとの整合を保っているかだけを検証する。
    """

    def test_undefined_when_no_goal_linked(self, activity_id):
        """紐づけ行が無いactivityはlabel=undefinedを返す"""
        result = collect_and_assemble(activity_id)

        assert result["control"]["goal"]["label"] == "undefined"
        assert result["control"]["goal"]["next"]["rule"] == 4
        assert result["control"]["goal"]["next"]["actor"] == "claude"

    def test_not_needed_when_waived(self, activity_id):
        """不要印を付けたactivityはlabel=not_needed・reasonを返す"""
        gs.set_goal(activity_id, {"waiver": "常駐タスクのため終了条件なし"})

        result = collect_and_assemble(activity_id)

        assert result["control"]["goal"]["label"] == "not_needed"
        assert result["control"]["goal"]["reason"] == "常駐タスクのため終了条件なし"

    def test_goal_linked_active_label_and_next(self, activity_id):
        """goal付きのactivityはlabel=activeとgoalの本体（handle/statement/next）を返す"""
        gs.set_goal(
            activity_id,
            {
                "new": {
                    "handle": "checkin-wiring-g1",
                    "statement": "終わりの一文",
                    "conditions": [{"statement": "条件1", "actor": "claude"}],
                }
            },
        )

        result = collect_and_assemble(activity_id)

        goal = result["control"]["goal"]
        assert goal["label"] == "active"
        assert goal["handle"] == "checkin-wiring-g1"
        assert goal["statement"] == "終わりの一文"
        assert goal["next"]["rule"] == 11

    def test_judged_goal_check_in_reopens_activity_and_reports_rule5(self, activity_id):
        """判定済みgoalのactivityにcheck_inすると、statusはin_progressに戻り、
        closed_*は残ったまま、goalブロックは規則5（判定済み）を返す"""
        set_result = gs.set_goal(
            activity_id,
            {
                "new": {
                    "handle": "checkin-wiring-g2",
                    "statement": "終わりの一文2",
                    "conditions": [
                        {"statement": "条件1", "actor": "claude", "state": "satisfied", "note": "済"}
                    ],
                }
            },
        )
        goal_id = set_result["goal_id_raw"]
        judge_result = gs.judge_goal(goal_id, "achieved", note="達成した")
        assert "error" not in judge_result

        conn = get_connection()
        try:
            row_before = conn.execute(
                "SELECT status, closed_by, closed_reason FROM activities WHERE id = ?",
                (activity_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row_before["status"] == "completed"
        assert row_before["closed_by"] == "goal_judge"

        result = collect_and_assemble(activity_id)

        assert result["anchor"]["activity"]["status"] == "in_progress"
        assert result["control"]["goal"]["label"] == "closed"
        assert result["control"]["goal"]["next"]["rule"] == 5

        conn = get_connection()
        try:
            row_after = conn.execute(
                "SELECT closed_by, closed_reason FROM activities WHERE id = ?",
                (activity_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row_after["closed_by"] == "goal_judge"
        assert row_after["closed_reason"] == "達成した"

    def test_exception_in_goal_block_does_not_break_check_in(self, activity_id, monkeypatch):
        """goalブロックの組み立てで例外が出ても、check_inの他のキーは失われず、
        goalキーにエラーの形が載る。anchor.activityだけでなくenv.coverage・
        env.sessionも残ることを見る"""
        monkeypatch.setattr(
            gs,
            "build_goal_block_for_activity",
            lambda conn, aid: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        result = collect_and_assemble(activity_id)

        assert "error" not in result
        assert result["control"]["goal"] == {
            "error": {
                "code": "DATABASE_ERROR",
                "message": "goal ブロックを組み立てられなかった",
            }
        }
        assert "activity" in result["anchor"]
        assert result["anchor"]["activity"]["status"] == "in_progress"
        assert "coverage" in result["env"]
        assert "session" in result["env"]

    def test_exception_in_goal_block_records_machine_error_signal(self, activity_id, monkeypatch):
        """goalブロック組み立ての例外は、check_inの接続を渡したrecord_signalで
        machine_errorとして記録され、check_inのコミット後にDBへ残る。"""
        monkeypatch.setattr(
            gs,
            "build_goal_block_for_activity",
            lambda conn, aid: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        collect_and_assemble(activity_id)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT kind, source, detail FROM signal_events "
                "WHERE kind = 'machine_error' AND source = 'tool:check_in'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert "boom" in row["detail"]

    def test_exception_in_goal_block_reuses_check_in_connection_for_signal(
        self, activity_id, monkeypatch
    ):
        """goalブロック組み立て失敗時のrecord_signalは、check_inが開いた接続を
        そのまま渡す。別接続を新たに開くcapture_signal_safeは使わない
        （check_inの接続が書き込みを保留していてもbusy_timeoutまで
        待たないための取り決め）。同一接続の逐次実行は自分自身の保留中の
        書き込みでは絶対にブロックしないため、この配線自体が待ちなしを保証する。
        """
        monkeypatch.setattr(
            gs,
            "build_goal_block_for_activity",
            lambda conn, aid: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        connections_created = []
        original_get_connection = checkin_tier_service.get_connection

        def spy_get_connection(*args, **kwargs):
            conn = original_get_connection(*args, **kwargs)
            connections_created.append(conn)
            return conn

        signal_conns = []
        original_record_signal = checkin_tier_service.record_signal

        def spy_record_signal(*args, **kwargs):
            signal_conns.append(kwargs.get("conn"))
            return original_record_signal(*args, **kwargs)

        monkeypatch.setattr(checkin_tier_service, "get_connection", spy_get_connection)
        monkeypatch.setattr(checkin_tier_service, "record_signal", spy_record_signal)

        collect_and_assemble(activity_id)

        assert len(signal_conns) == 1
        # check_in自身が開いた接続はこの1本だけであり、record_signalに渡された
        # connはその同じオブジェクトである（capture_signal_safeのような
        # 追加のget_connection()呼び出しは発生していない）
        assert connections_created == [signal_conns[0]]

    def test_goal_block_folds_to_budget_without_truncating_statement_or_conditions(self, activity_id):
        """goalブロックが目安の800字を超えるとき、remainingが件数表示に畳まれる。
        goalのstatementと畳まれていない条件文は切り詰められない。"""
        long_statement = "終わりの一文を長くする" * 40  # 480字程度
        long_condition_texts = [f"条件{i}を長くするための繰り返し文言" * 6 for i in range(5)]
        set_result = gs.set_goal(
            activity_id,
            {
                "new": {
                    "handle": "checkin-wiring-budget",
                    "statement": long_statement,
                    "conditions": [
                        {"statement": text, "actor": "claude"} for text in long_condition_texts
                    ],
                }
            },
        )
        assert "error" not in set_result

        result = collect_and_assemble(activity_id)
        goal = result["control"]["goal"]

        # 折り畳み前提のデータ量になっていること自体を確かめる（前提の自己検証）
        assert len(json.dumps(goal, ensure_ascii=False)) <= 800 or isinstance(goal.get("remaining"), str)
        assert goal["statement"] == long_statement
        assert isinstance(goal["remaining"], str)
        assert goal["remaining"].endswith("件")
        assert "others" not in goal
        # 畳まれる前に選ばれていたはずのnext（規則11、id順先頭）の文は
        # 切り詰められずそのまま残る
        assert goal["next"]["what"] == long_condition_texts[0]


def _make_activity(title: str, *, status: str | None = None) -> int:
    result = add_activity(title=title, description=f"{title}の説明", tags=DEFAULT_TAGS, check_in=False)
    activity_id = result["activity_id"]
    if status is not None:
        update_activity(activity_id, status=status)
    return activity_id


def _add_ask(conn, activity_id: int, question: str, *, answered: bool = False, answer_body: str = "") -> int:
    result = add_ask_with_conn(conn, question, [activity_id], DEFAULT_TAGS)
    assert "error" not in result
    ask_id = result["id"]
    if answered:
        conn.execute(
            "UPDATE asks SET status = 'answered', answer_body = ?, answered_at = CURRENT_TIMESTAMP WHERE id = ?",
            (answer_body, ask_id),
        )
    conn.commit()
    return ask_id


class TestPinned:
    def test_pinned_set_collected_correctly(self, temp_db):
        topic = add_topic(title="pinned検証用トピック", description="pinned検証用", tags=DEFAULT_TAGS)
        decision = add_decision("決定X", "理由X", topic["topic_id"])
        log = add_log(topic["topic_id"], title="ログX", content="本文X")
        material = add_material(title="資材X", content="内容X", source="test", tags=DEFAULT_TAGS)

        activity_id = _make_activity("pinned検証")
        add_pin("activity", activity_id, "decision", decision["decision_id"])
        add_pin("activity", activity_id, "log", log["log_id"])
        add_pin("activity", activity_id, "material", material["material_id"])

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="pinned-check")

        assert "error" not in result
        pinned = result["anchor"]["pinned"]
        assert set(pinned.keys()) == {"decisions", "logs", "materials"}
        assert pinned["decisions"][0]["id_raw"] == decision["decision_id"]
        assert pinned["logs"][0]["id_raw"] == log["log_id"]
        assert pinned["materials"][0]["id_raw"] == material["material_id"]


class TestAsks:
    def test_asks_content_under_cap(self, temp_db):
        activity_id = _make_activity("asks検証")
        conn = get_connection()
        try:
            _add_ask(conn, activity_id, "未回答の質問")
            _add_ask(conn, activity_id, "回答済み未トリアージの質問", answered=True, answer_body="回答本文")
        finally:
            conn.close()

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="asks-check")

        asks = result["control"]["asks"]
        assert len(asks["awaiting_answer"]) == 1
        assert len(asks["awaiting_triage"]) == 1
        assert asks["awaiting_triage"][0]["answer_body"] == "回答本文"
        assert "more" not in asks

    def test_asks_over_cap_folds_to_more_with_pointer_and_truncates_answer_body(self, temp_db):
        activity_id = _make_activity("asks上限超過検証")
        conn = get_connection()
        try:
            for i in range(4):
                _add_ask(conn, activity_id, f"未回答の質問{i}")
            long_body = "あ" * 500
            for i in range(3):
                _add_ask(conn, activity_id, f"回答済みの質問{i}", answered=True, answer_body=long_body)
        finally:
            conn.close()

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="asks-overflow")

        asks = result["control"]["asks"]
        kept_total = len(asks["awaiting_answer"]) + len(asks["awaiting_triage"])
        assert kept_total == checkin_tier_service.ASKS_MAX
        assert asks["more"] == 7 - checkin_tier_service.ASKS_MAX
        assert asks["next"] == [{"tool": "get_asks", "args": {"blocking_activity_id": activity_id}}]
        for item in asks["awaiting_triage"]:
            assert len(item["answer_body"]) == checkin_tier_service.ASK_ANSWER_BODY_MAX_CHARS
            assert item["answer_truncated"] is True


class TestDependencies:
    def test_dependencies_content_under_cap(self, temp_db):
        dep = _make_activity("依存先")
        main_id = _make_activity("依存関係検証")
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (main_id, dep),
            )
            conn.commit()
        finally:
            conn.close()

        result = checkin_tier_service.collect_and_assemble(main_id, session_id="deps-check")

        deps = result["control"]["dependencies"]
        assert len(deps) == 1
        assert deps[0]["id_raw"] == dep

    def test_dependencies_over_cap_folds_to_items_and_more_with_pointer(self, temp_db):
        main_id = _make_activity("依存関係上限超過検証")
        conn = get_connection()
        try:
            dep_ids = [_make_activity(f"依存先{i}") for i in range(12)]
            for dep_id in dep_ids:
                conn.execute(
                    "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                    (main_id, dep_id),
                )
            conn.commit()
        finally:
            conn.close()

        result = checkin_tier_service.collect_and_assemble(main_id, session_id="deps-overflow")

        deps = result["control"]["dependencies"]
        assert len(deps["items"]) == checkin_tier_service.DEPENDENCIES_MAX
        assert deps["more"] == 12 - checkin_tier_service.DEPENDENCIES_MAX
        assert deps["next"] == [{"tool": "get_map", "args": {"entity_type": "activity", "entity_id": main_id}}]


class TestGoal:
    def test_goal_block_active_label(self, temp_db):
        """control.goalが、goal_service.build_goal_block_for_activityの出力を
        そのまま格納していることを、goalブロック全体を突き合わせて確認する
        （label/handle/statementの3項目だけでなく、progress/claude/next/remaining
        等も含めて一致することを見る）。
        """
        activity_id = _make_activity("goal検証")
        gs.set_goal(
            activity_id,
            {"new": {"handle": "tier-goal-check", "statement": "終わりの一文", "conditions": [
                {"statement": "条件1", "actor": "claude"}
            ]}},
        )

        conn = get_connection()
        try:
            expected_goal = gs.build_goal_block_for_activity(conn, activity_id)
        finally:
            conn.close()

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="goal-check")

        assert result["control"]["goal"] == expected_goal
        assert expected_goal["label"] == "active"

    def test_goal_failure_is_isolated(self, temp_db, monkeypatch):
        """goalブロック組み立てで例外が出ても、他のキーは失われずgoalにerrorの形が
        載る。anchor.activityだけでなくenv.coverage・env.sessionも残ることを見る。
        machine_errorのsignalがcheck_inの接続で記録される。
        """
        activity_id = _make_activity("goal失敗検証")
        monkeypatch.setattr(
            gs, "build_goal_block_for_activity", lambda conn, aid: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="goal-fail-check")

        expected_error_goal = {"error": {"code": "DATABASE_ERROR", "message": "goal ブロックを組み立てられなかった"}}
        assert result["control"]["goal"] == expected_error_goal
        assert result["anchor"]["activity"]["status"] == "in_progress"
        assert "coverage" in result["env"]
        assert "session" in result["env"]

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT detail FROM signal_events WHERE kind = 'machine_error' AND source = 'tool:check_in'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert "boom" in rows[0]["detail"]


class TestStatusTransition:
    """completedのactivityへのcheck_inが、asks収集より先にin_progressへ遷移することを
    固定する（既決: 遷移が先でないと、再オープンしたactivityのブロックaskが
    completed扱いで除外されたままになる）。
    """

    def test_reopens_completed_activity_and_delivers_blocking_ask(self, temp_db):
        activity_id = _make_activity("完了済み再開検証")
        conn = get_connection()
        try:
            _add_ask(conn, activity_id, "完了済みactivityをブロックする質問")
        finally:
            conn.close()
        update_activity(activity_id, status="completed")

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="reopen-check")

        assert result["anchor"]["activity"]["status"] == "in_progress"
        assert "asks" in result["control"]
        assert len(result["control"]["asks"]["awaiting_answer"]) == 1


class TestSessionAlias:
    """セッション別名レジストリへの登録を検証する。"""

    @pytest.fixture(autouse=True)
    def _isolate_registry_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv(session_registry_service.REGISTRY_PATH_ENV, str(tmp_path / "session_aliases.json"))

    def _stub_world(self, monkeypatch, bridge_session_id: str, cli_session_id: str, cli_pid: int, name: str):
        def resolve(bsid):
            if bsid != bridge_session_id:
                return None
            return {"cli_pid": cli_pid, "cli_session_id": cli_session_id, "name": name, "cwd": None, "cli_status": None}

        monkeypatch.setattr(session_identity, "resolve_cli_session", resolve)
        monkeypatch.setattr(session_registry_service, "is_process_alive", lambda pid: pid == cli_pid)
        monkeypatch.setattr(
            session_registry_service.cli_session,
            "read_cli_session",
            lambda pid: resolve(bridge_session_id) if pid == cli_pid else None,
        )
        monkeypatch.setattr(session_identity, "get_caller_session_id", lambda: bridge_session_id)

    def test_session_field_populated_when_cli_resolved(self, temp_db, monkeypatch):
        activity_id = _make_activity("セッション別名検証")
        self._stub_world(monkeypatch, "bridge-new", "cli-new", 200, "workspace-new")

        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="bridge-new")

        assert result["env"]["session"] == {
            "name": "workspace-new",
            "alias": "セッション別名検証",
            "alias_collision": False,
        }

    def test_session_field_reports_unresolved_when_bridge_id_missing(self, temp_db, monkeypatch):
        monkeypatch.setattr(session_identity, "get_caller_session_id", lambda: None)

        activity_id = _make_activity("セッション別名未解決検証")
        result = checkin_tier_service.collect_and_assemble(activity_id, session_id="unresolved-check")

        assert result["env"]["session"] == {"registered": False, "reason": "cli_unresolved"}
