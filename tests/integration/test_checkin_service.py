"""check-inサービスの統合テスト"""
import os
import tempfile
import pytest
from src.db import init_database, get_connection
from src.services.activity_service import add_activity, update_activity
from tests.helpers import add_decision, add_log, retract_decision
from src.services.material_service import add_material, update_material
from src.services.pin_service import add_pin
from src.services.relation_service import add_relation
from src.services.topic_service import add_topic
from src.services.checkin_service import (
    check_in,
    DECISIONS_FULL_LIMIT,
)
from src.services.hint_service import (
    RECOMPOSE_BOOTSTRAP_THRESHOLD as _RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD,
    RECOMPOSE_DELTA_THRESHOLD as _RECOMPOSE_HINT_DELTA_THRESHOLD,
)
from src.services.tag_service import _injected_tags


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        # tag_notes注入済みセットをリセット（テスト間の干渉防止）
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


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
    """check_inの統合テスト"""

    def test_check_in_success(self, activity_id):
        """check-inが成功し、必須フィールドがすべて返る"""
        result = check_in(activity_id)

        assert "error" not in result
        assert "activity" in result
        assert result["activity"]["id_raw"] == activity_id
        assert result["activity"]["title"] == "[作業] タグnotesカラム追加"
        assert result["activity"]["description"] == "タグnotesカラムを追加する作業"
        assert result["activity"]["status"] == "in_progress"
        assert "tags" in result["activity"]
        assert "tag_notes" in result
        assert "materials" in result
        assert "recent_decisions" in result
        assert "summary" in result

    def test_check_in_status_updated_to_in_progress(self, activity_id):
        """pendingのアクティビティがin_progressに自動更新される"""
        result = check_in(activity_id)

        assert "error" not in result
        assert result["activity"]["status"] == "in_progress"

    def test_check_in_already_in_progress(self, activity_id):
        """すでにin_progressの場合、status変更なしでcheck-in成功"""
        # 先にin_progressに変更
        update_activity(activity_id, status="in_progress")

        result = check_in(activity_id)

        assert "error" not in result
        assert result["activity"]["status"] == "in_progress"

    def test_check_in_completed_activity(self, activity_id):
        """completedのアクティビティもin_progressに戻る"""
        update_activity(activity_id, status="completed")

        result = check_in(activity_id)

        assert "error" not in result
        assert result["activity"]["status"] == "in_progress"

    def test_check_in_not_found(self, temp_db):
        """存在しないactivity_idでNOT_FOUNDエラーになる"""
        result = check_in(9999)

        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"
        assert "9999" in result["error"]["message"]

    def test_check_in_no_related_topics_when_no_relations(self, activity_id):
        """リレーションがない場合、related_topicsが結果に含まれない"""
        result = check_in(activity_id)

        assert "error" not in result
        # リレーションが未設定のため、related_topicsは省略される
        assert "related_topics" not in result

    def test_check_in_materials_empty(self, activity_id):
        """materials 0件の場合、空リストが返る"""
        result = check_in(activity_id)

        assert "error" not in result
        assert result["materials"] == []

    def test_check_in_with_materials(self, activity_id):
        """materialsがある場合、relationsテーブル経由でカタログ形式で返る"""
        add_material("設計書", "# 設計\n詳細内容", ["domain:test"], "テスト用データ",
                     related=[{"type": "activity", "ids": [activity_id]}])
        m2 = add_material("調査結果", "# 調査\n結果内容", ["domain:test"], "テスト用データ",
                          related=[{"type": "activity", "ids": [activity_id]}])

        result = check_in(activity_id)

        assert "error" not in result
        assert len(result["materials"]) == 2
        # カタログ形式: id_raw, title, snippet, source, created_at (contentなし)
        for m in result["materials"]:
            assert "id_raw" in m
            assert "id" not in m
            assert "title" in m
            assert "snippet" in m
            assert "source" in m
            assert "created_at" in m
            assert "content" not in m
            assert m["source"] == "テスト用データ"
        # snippetの値が正しい
        assert result["materials"][0]["snippet"] == "# 設計\n詳細内容"
        assert result["materials"][1]["snippet"] == "# 調査\n結果内容"

    def test_check_in_materials_snippet_truncated(self, activity_id):
        """materialsのsnippetが200文字に切り詰められる"""
        long_content = "あ" * 250
        add_material("長い資材", long_content, ["domain:test"], "テスト用データ",
                      related=[{"type": "activity", "ids": [activity_id]}])

        result = check_in(activity_id)

        assert "error" not in result
        assert len(result["materials"]) == 1
        assert len(result["materials"][0]["snippet"]) == 200
        assert result["materials"][0]["snippet"] == "あ" * 200

    def test_check_in_recent_decisions_empty_without_relations(self, activity_id):
        """リレーションがない場合、recent_decisionsは空リスト"""
        result = check_in(activity_id)

        assert "error" not in result
        assert result["recent_decisions"] == []


class TestCheckInSummary:
    """summary文字列のフォーマット確認"""

    def test_summary_format_basic(self, activity_id):
        """summaryが仕様のフォーマットに従っている"""
        result = check_in(activity_id)

        assert "error" not in result
        summary = result["summary"]
        lines = summary.split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("check-in: ")
        assert "[作業] タグnotesカラム追加" in lines[0]
        assert "intent:" in lines[1]

    def test_summary_intent_from_tag(self, activity_with_intent):
        """intent:タグがある場合、summaryにintent値が表示される"""
        result = check_in(activity_with_intent)

        assert "error" not in result
        assert "intent: design" in result["summary"]

    def test_summary_intent_unset(self, activity_id):
        """intent:タグがない場合、(未設定)と表示される"""
        result = check_in(activity_id)

        assert "error" not in result
        assert "intent: (未設定)" in result["summary"]


class TestCheckInTagNotes:
    """tag_notes注入の確認"""

    def test_tag_notes_injected(self, temp_db):
        """notesを持つタグがtag_notesに含まれる"""
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

        result = check_in(activity["activity_id"])

        assert "error" not in result
        assert len(result["tag_notes"]) == 1
        assert result["tag_notes"][0]["tag"] == "domain:withnotes"
        assert result["tag_notes"][0]["notes"] == "重要な教訓"

    def test_tag_notes_empty_when_no_notes(self, activity_id):
        """notesがないタグの場合、tag_notesは空リスト"""
        result = check_in(activity_id)

        assert "error" not in result
        assert result["tag_notes"] == []

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
        result1 = check_in(aid)
        assert "error" not in result1
        intent_notes1 = [n for n in result1["tag_notes"] if n["tag"] == "intent:design"]
        assert len(intent_notes1) == 1

        # 2回目: intent: は常時注入なので再度返る
        result2 = check_in(aid)
        assert "error" not in result2
        intent_notes2 = [n for n in result2["tag_notes"] if n["tag"] == "intent:design"]
        assert len(intent_notes2) == 1

    def test_non_intent_tag_notes_injected_once(self, temp_db):
        """intent:以外のタグのnotesはセッション初回のみ注入される"""
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
        result1 = check_in(aid)
        assert "error" not in result1
        domain_notes1 = [n for n in result1["tag_notes"] if n["tag"] == "domain:once"]
        assert len(domain_notes1) == 1

        # 2回目: domain: は通常タグなので注入されない
        result2 = check_in(aid)
        assert "error" not in result2
        domain_notes2 = [n for n in result2["tag_notes"] if n["tag"] == "domain:once"]
        assert len(domain_notes2) == 0



class TestCheckInRelations:
    """リレーション関連のcheck-inテスト"""

    def test_related_activities_returned(self, temp_db):
        """関連アクティビティがrelated_activitiesに含まれる"""
        a1 = add_activity(title="親タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        a2 = add_activity(title="子タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a1["activity_id"], [{"type": "activity", "ids": [a2["activity_id"]]}])

        result = check_in(a1["activity_id"])

        assert "error" not in result
        assert "related_activities" in result
        assert len(result["related_activities"]) == 1
        assert result["related_activities"][0]["id_raw"] == a2["activity_id"]
        assert result["related_activities"][0]["title"] == "子タスク"

    def test_no_related_activities_key_when_empty(self, activity_id):
        """関連アクティビティがない場合、related_activitiesキーは省略される"""
        result = check_in(activity_id)

        assert "error" not in result
        assert "related_activities" not in result

    def test_single_related_topic_sets_topic_key(self, temp_db):
        """関連トピックが1件の場合、topicキーにdictがセットされる"""
        topic = add_topic(title="テストトピック", description="Desc", tags=DEFAULT_TAGS)
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert "topic" in result
        assert result["topic"]["id_raw"] == topic["topic_id"]
        assert result["related_topics"] == [result["topic"]]

    def test_multiple_related_topics_no_topic_key(self, temp_db):
        """関連トピックが複数の場合、topicキーは省略される"""
        t1 = add_topic(title="トピック1", description="Desc", tags=DEFAULT_TAGS)
        t2 = add_topic(title="トピック2", description="Desc", tags=DEFAULT_TAGS)
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [t1["topic_id"], t2["topic_id"]]}])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert "topic" not in result
        assert len(result["related_topics"]) == 2

    def test_decisions_limited_to_max(self, temp_db):
        """decisionsがDECISIONS_FULL_LIMIT件に制限される"""
        topic = add_topic(title="決定多数トピック", description="Desc", tags=DEFAULT_TAGS)
        for i in range(DECISIONS_FULL_LIMIT + 5):
            add_decision(decision=f"決定事項{i}", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert len(result["recent_decisions"]) == DECISIONS_FULL_LIMIT

    def test_related_topics_include_gravity_counts(self, temp_db):
        """related_topicsの各topicにdecisions_count/materials_countが含まれる"""
        topic = add_topic(title="重力テスト", description="Desc", tags=DEFAULT_TAGS)
        tid = topic["topic_id"]
        # decisions 2件
        add_decision(decision="決定1", reason="理由", topic_id=tid)
        add_decision(decision="決定2", reason="理由", topic_id=tid)
        # material 1件を直接紐づけ
        add_material("資材1", "内容", DEFAULT_TAGS, "src", related=[{"type": "topic", "ids": [tid]}])
        # activity経由のmaterialはmaterials_countに含まれないことを確認するためのダミー
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [tid]}])
        add_material(
            "activity経由資材", "内容", DEFAULT_TAGS, "src",
            related=[{"type": "activity", "ids": [a["activity_id"]]}],
        )

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert len(result["related_topics"]) == 1
        rt = result["related_topics"][0]
        assert rt["id_raw"] == tid
        assert rt["decisions_count"] == 2
        # topic直接紐づけは1件のみ（activity経由のmaterialは含まない）
        assert rt["materials_count"] == 1

    def test_related_topics_zero_counts_present(self, temp_db):
        """decisions/materialsがゼロのtopicでもdecisions_count=0, materials_count=0が返る"""
        topic = add_topic(title="空のトピック", description="Desc", tags=DEFAULT_TAGS)
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert len(result["related_topics"]) == 1
        rt = result["related_topics"][0]
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

        result = check_in(a["activity_id"])

        assert "error" not in result
        rt = result["related_topics"][0]
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

        result = check_in(a["activity_id"])

        assert "error" not in result
        by_id = {rt["id_raw"]: rt for rt in result["related_topics"]}
        assert by_id[tid1]["decisions_count"] == 2
        assert by_id[tid1]["materials_count"] == 1
        assert by_id[tid2]["decisions_count"] == 1
        assert by_id[tid2]["materials_count"] == 2


class TestCheckInCoverage:
    """coverageフィールドのテスト"""

    def test_coverage_field_exists(self, activity_id):
        """coverageフィールドがトップレベルに含まれる"""
        result = check_in(activity_id)

        assert "error" not in result
        assert "coverage" in result
        assert "decisions" in result["coverage"]
        assert "materials" in result["coverage"]
        assert "logs" in result["coverage"]

    def test_coverage_is_first_key(self, activity_id):
        """coverageがレスポンスの最初のキーである"""
        result = check_in(activity_id)

        assert "error" not in result
        keys = list(result.keys())
        assert keys[0] == "coverage"

    def test_coverage_no_relations_format(self, activity_id):
        """リレーションなしの場合、coverage分母は0"""
        result = check_in(activity_id)

        assert "error" not in result
        assert result["coverage"]["decisions"] == "0/0"
        assert result["coverage"]["materials"] == "0/0"
        assert result["coverage"]["logs"] == "0/0"

    def test_coverage_with_decisions(self, temp_db):
        """decisionsがある場合、coverageの分母に件数が反映される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        for i in range(3):
            add_decision(decision=f"決定{i}", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = check_in(a["activity_id"])

        assert "error" not in result
        # 分子: min(3, DECISIONS_FULL_LIMIT) = 3, 分母: 3
        assert result["coverage"]["decisions"] == "3/3"

    def test_coverage_decisions_exceeds_limit(self, temp_db):
        """decisions総数がDECISIONS_FULL_LIMITを超えた場合、分子は制限値になる"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        total = DECISIONS_FULL_LIMIT + 5
        for i in range(total):
            add_decision(decision=f"決定{i}", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert result["coverage"]["decisions"] == f"{DECISIONS_FULL_LIMIT}/{total}"

    def test_coverage_with_materials(self, activity_id):
        """materialsがある場合、coverageの分母に件数が反映される"""
        add_material("資材1", "内容1", DEFAULT_TAGS, "テスト用データ", related=[{"type": "activity", "ids": [activity_id]}])
        add_material("資材2", "内容2", DEFAULT_TAGS, "テスト用データ", related=[{"type": "activity", "ids": [activity_id]}])

        result = check_in(activity_id)

        assert "error" not in result
        assert result["coverage"]["materials"] == "2/2"

    def test_coverage_logs_includes_latest(self, temp_db):
        """logsの分子に最新ログ1件が加算される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        for i in range(3):
            add_log(topic_id=topic["topic_id"], title=f"ログ{i}", content=f"内容{i}")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert result["coverage"]["logs"] == "1/3"

    def test_coverage_zero_related_topics(self, activity_id):
        """関連topic 0件の場合、coverage "0/0"が返る（Edge case）"""
        result = check_in(activity_id)

        assert "error" not in result
        assert result["coverage"]["decisions"] == "0/0"
        assert result["coverage"]["materials"] == "0/0"
        assert result["coverage"]["logs"] == "0/0"

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

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert "pinned" in result
        assert len(result["pinned"]["decisions"]) == 1
        # coverageは関連topic配下のdecisionのみ: 通常1件/全体1件。pin注入分は加算されない
        assert result["coverage"]["decisions"] == "1/1"


class TestCheckInLogsCatalog:
    """logsカタログのテスト"""

    def test_logs_field_exists(self, activity_id):
        """logsフィールドが常に存在する"""
        result = check_in(activity_id)

        assert "error" not in result
        assert "logs" in result

    def test_logs_empty_without_relations(self, activity_id):
        """リレーションなしの場合、latest_logはNone、logsは空リスト"""
        result = check_in(activity_id)

        assert "error" not in result
        assert result["latest_log"] is None
        assert result["logs"] == []

    def test_latest_log_has_content(self, temp_db):
        """最新ログ1件がcontent付きでlatest_logに返る"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        add_log(topic_id=topic["topic_id"], title="初回議論", content="詳細な内容")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert result["latest_log"] is not None
        assert result["latest_log"]["title"] == "初回議論"
        assert result["latest_log"]["content"] == "詳細な内容"
        assert result["logs"] == []

    def test_logs_catalog_excludes_latest(self, temp_db):
        """最新1件以外のlogsはid+titleのカタログとして返る"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        add_log(topic_id=topic["topic_id"], title="古いログ", content="古い内容")
        add_log(topic_id=topic["topic_id"], title="新しいログ", content="新しい内容")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert result["latest_log"]["title"] == "新しいログ"
        assert result["latest_log"]["content"] == "新しい内容"
        assert len(result["logs"]) == 1
        assert result["logs"][0]["title"] == "古いログ"
        assert "content" not in result["logs"][0]

    def test_logs_catalog_multiple_topics(self, temp_db):
        """複数topicのlogsが集約される（最新1件がlatest_log、残りがカタログ）"""
        t1 = add_topic(title="トピック1", description="Desc", tags=DEFAULT_TAGS)
        t2 = add_topic(title="トピック2", description="Desc", tags=DEFAULT_TAGS)
        add_log(topic_id=t1["topic_id"], title="ログA", content="内容A")
        add_log(topic_id=t2["topic_id"], title="ログB", content="内容B")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [t1["topic_id"], t2["topic_id"]]}])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert result["latest_log"] is not None
        assert len(result["logs"]) == 1
        all_titles = {result["latest_log"]["title"]} | {l["title"] for l in result["logs"]}
        assert "ログA" in all_titles
        assert "ログB" in all_titles


class TestCheckInDependencies:
    """check-in結果のdependenciesフィールドのテスト"""

    def test_dependencies_present_when_depends_on_exists(self, temp_db):
        """depends_on関係がある場合、dependenciesフィールドが結果に含まれる"""
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

        result = check_in(main["activity_id"])

        assert "error" not in result
        assert "dependencies" in result
        assert len(result["dependencies"]) == 1
        assert result["dependencies"][0]["id_raw"] == dep["activity_id"]
        assert result["dependencies"][0]["title"] == "依存先タスク"
        assert result["dependencies"][0]["status"] == "pending"

    def test_dependencies_absent_when_no_depends_on(self, activity_id):
        """depends_on関係がない場合、dependenciesフィールドは省略される"""
        result = check_in(activity_id)

        assert "error" not in result
        assert "dependencies" not in result

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

        result = check_in(main["activity_id"])

        assert "error" not in result
        assert len(result["dependencies"]) == 2
        dep_ids = {d["id_raw"] for d in result["dependencies"]}
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

        result = check_in(main["activity_id"])

        assert "error" not in result
        assert "dependencies" in result
        assert result["dependencies"][0]["status"] == "completed"

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

        result = check_in(main["activity_id"])

        assert "error" not in result
        assert result["dependencies"][0]["status"] == "in_progress"


class TestCheckInPinned:
    """pinsテーブル経由のpinned target注入テスト"""

    def test_no_pinned_field_when_nothing_pinned(self, temp_db):
        """pinsテーブルに対象activity向けのpinがない場合、pinnedフィールドは省略される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        add_decision(decision="通常の決定", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert "pinned" not in result

    def test_activity_source_pin_injects_decision(self, temp_db):
        """source=activityのpinsテーブルエントリが、check-in時にpinned.decisionsにcontent付きで注入される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d = add_decision(decision="重要な決定", reason="根本的な理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        # pinsにsource=activityでdecisionをpin
        add_pin("activity", a["activity_id"], "decision", d["decision_id"])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert "pinned" in result
        assert "decisions" in result["pinned"]
        assert len(result["pinned"]["decisions"]) == 1
        assert result["pinned"]["decisions"][0]["title"] == "重要な決定"
        assert result["pinned"]["decisions"][0]["reason"] == "根本的な理由"

    def test_tag_source_pin_injects_decision(self, temp_db):
        """source=tag（activity自身のtag）のpinsテーブルエントリが、check-in時にpinned.decisionsに注入される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d = add_decision(decision="タグ経由重要決定", reason="根拠", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        # pinsにsource=tagでdecisionをpin（domain:testタグ）
        add_pin("tag", "domain:test", "decision", d["decision_id"])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert "pinned" in result
        assert "decisions" in result["pinned"]
        assert len(result["pinned"]["decisions"]) == 1
        assert result["pinned"]["decisions"][0]["title"] == "タグ経由重要決定"

    def test_pinned_decisions_included_in_recent_decisions(self, temp_db):
        """pinsテーブルでpinされたdecisionはrecent_decisionsにも通常通り含まれる（除外されない）"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d = add_decision(decision="ピン済み決定", reason="理由", topic_id=topic["topic_id"])
        add_decision(decision="通常の決定", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])
        # decisionをpinする
        add_pin("activity", a["activity_id"], "decision", d["decision_id"])

        result = check_in(a["activity_id"])

        assert "error" not in result
        # recent_decisionsにはピン済み・非ピンの両方が含まれる（pinned列による除外なし）
        assert len(result["recent_decisions"]) == 2
        titles = {dec["title"] for dec in result["recent_decisions"]}
        assert "ピン済み決定" in titles
        assert "通常の決定" in titles

    def test_activity_source_pin_injects_log(self, temp_db):
        """source=activityのpinsテーブルエントリが、check-in時にpinned.logsにcontent付きで注入される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        log = add_log(topic_id=topic["topic_id"], title="方向転換ログ", content="## 経緯\n重要な方向転換")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_pin("activity", a["activity_id"], "log", log["log_id"])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert "pinned" in result
        assert "logs" in result["pinned"]
        assert len(result["pinned"]["logs"]) == 1
        assert result["pinned"]["logs"][0]["title"] == "方向転換ログ"
        assert result["pinned"]["logs"][0]["content"] == "## 経緯\n重要な方向転換"

    def test_pinned_log_also_appears_in_latest_log(self, temp_db):
        """pinsテーブルでpinされたlogはlatest_logにも通常通り含まれる（除外されない）"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        log1 = add_log(topic_id=topic["topic_id"], title="ピン済みログ", content="内容1")
        add_log(topic_id=topic["topic_id"], title="新しいログ", content="内容2")
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", a["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])
        # log1をpinするが、IDが小さい（古い）ため latest_log には新しい方が来る
        add_pin("activity", a["activity_id"], "log", log1["log_id"])

        result = check_in(a["activity_id"])

        assert "error" not in result
        # latest_logには最新のログが入る（pinned列による除外なし）
        assert result["latest_log"]["title"] == "新しいログ"
        # logsカタログにはpinされたログが残る
        assert len(result["logs"]) == 1
        assert result["logs"][0]["title"] == "ピン済みログ"

    def test_activity_source_pin_injects_material(self, temp_db):
        """source=activityのpinsテーブルエントリが、check-in時にpinned.materialsにcontent付きで注入される"""
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        m = add_material("設計書", "# 設計\n詳細な内容", DEFAULT_TAGS, "テスト用データ",
                         related=[{"type": "activity", "ids": [a["activity_id"]]}])
        add_pin("activity", a["activity_id"], "material", m["material_id"])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert "pinned" in result
        assert "materials" in result["pinned"]
        assert len(result["pinned"]["materials"]) == 1
        assert result["pinned"]["materials"][0]["title"] == "設計書"
        assert result["pinned"]["materials"][0]["content"] == "# 設計\n詳細な内容"
        assert result["pinned"]["materials"][0]["source"] == "テスト用データ"

    def test_pinned_material_also_appears_in_materials(self, temp_db):
        """pinsテーブルでpinされたmaterialはmaterialsフィールドにも通常通り含まれる（除外されない）"""
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        m1 = add_material("ピン資材", "内容1", DEFAULT_TAGS, "テスト用データ",
                          related=[{"type": "activity", "ids": [a["activity_id"]]}])
        add_material("通常資材", "内容2", DEFAULT_TAGS, "テスト用データ",
                     related=[{"type": "activity", "ids": [a["activity_id"]]}])
        add_pin("activity", a["activity_id"], "material", m1["material_id"])

        result = check_in(a["activity_id"])

        assert "error" not in result
        # materialsにはpin済みも非ピンも両方含まれる（pinned列による除外なし）
        assert len(result["materials"]) == 2
        titles = {m["title"] for m in result["materials"]}
        assert "ピン資材" in titles
        assert "通常資材" in titles

    def test_activity_source_pin_injects_topic(self, temp_db):
        """source=activityのpinsテーブルエントリが、check-in時にpinned.topicsに注入される"""
        topic = add_topic(title="重要トピック", description="Desc", tags=DEFAULT_TAGS)
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_pin("activity", a["activity_id"], "topic", topic["topic_id"])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert "pinned" in result
        assert "topics" in result["pinned"]
        assert len(result["pinned"]["topics"]) == 1
        assert result["pinned"]["topics"][0]["id_raw"] == topic["topic_id"]
        assert result["pinned"]["topics"][0]["title"] == "重要トピック"

    def test_activity_source_pin_injects_activity(self, temp_db):
        """source=activityのpinsテーブルエントリが、check-in時にpinned.activitiesに注入される"""
        a1 = add_activity(title="メインタスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        a2 = add_activity(title="重要参照タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_pin("activity", a1["activity_id"], "activity", a2["activity_id"])

        result = check_in(a1["activity_id"])

        assert "error" not in result
        assert "pinned" in result
        assert "activities" in result["pinned"]
        assert len(result["pinned"]["activities"]) == 1
        assert result["pinned"]["activities"][0]["id_raw"] == a2["activity_id"]
        assert result["pinned"]["activities"][0]["title"] == "重要参照タスク"
        assert result["pinned"]["activities"][0]["status"] == "pending"

    def test_distinct_deduplication_when_multiple_routes(self, temp_db):
        """同一targetがtagソースとactivityソースの両方からpinされても、pinned結果に1件だけ注入される"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d = add_decision(decision="重複テスト決定", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        # activityソースとtagソースの両方からdecisionをpin
        add_pin("activity", a["activity_id"], "decision", d["decision_id"])
        add_pin("tag", "domain:test", "decision", d["decision_id"])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert "pinned" in result
        # (target_type, target_id) でDISTINCTされ、1件のみ
        assert len(result["pinned"]["decisions"]) == 1
        assert result["pinned"]["decisions"][0]["title"] == "重複テスト決定"

    def test_retracted_decision_excluded_from_pinned(self, temp_db):
        """retractされたdecisionはpinsテーブル経由でもpinned.decisionsに注入されない"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d = add_decision(decision="取り消し済み決定", reason="理由", topic_id=topic["topic_id"])
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_pin("activity", a["activity_id"], "decision", d["decision_id"])
        # decisionをretract
        retract_decision(d["decision_id"])

        result = check_in(a["activity_id"])

        assert "error" not in result
        # retractされているためpinnedキー自体が省略される（または decisions が空）
        assert "pinned" not in result or "decisions" not in result.get("pinned", {})

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

        result = check_in(a1["activity_id"])

        assert "error" not in result
        # a1はdomain:otherタグを持たないため、そのpinは注入されない
        assert "pinned" not in result

    def test_all_five_target_types_in_pinned(self, temp_db):
        """decision/log/material/topic/activityの5種すべてがpinnedフィールドに含まれる"""
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

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert "pinned" in result
        assert len(result["pinned"]["decisions"]) == 1
        assert len(result["pinned"]["logs"]) == 1
        assert len(result["pinned"]["materials"]) == 1
        assert len(result["pinned"]["topics"]) == 1
        assert len(result["pinned"]["activities"]) == 1

    def test_zero_key_omission_in_pinned(self, temp_db):
        """pinned結果で0件のキーは省略される"""
        a = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        # topicのみをpin（他のtypeはピンなし）
        add_pin("activity", a["activity_id"], "topic", topic["topic_id"])

        result = check_in(a["activity_id"])

        assert "error" not in result
        assert "pinned" in result
        assert "topics" in result["pinned"]
        # 0件のキーは省略される
        assert "decisions" not in result["pinned"]
        assert "logs" not in result["pinned"]
        assert "materials" not in result["pinned"]
        assert "activities" not in result["pinned"]


# recomposeナッジhintの境界条件テスト用。
# D#2780により対象は domain: namespace のみに限定された。
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

        result = check_in(activity_id)

        assert "error" not in result
        assert "hints" in result
        bootstrap_hints = [h for h in result["hints"] if "蓄積しています" in h]
        assert len(bootstrap_hints) == 1
        assert DOMAIN_TAG in bootstrap_hints[0]
        assert str(_RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD) in bootstrap_hints[0]

    def test_bootstrap_hint_absent_below_threshold(self, temp_db):
        """material未pinのtagで、decisionがブートストラップしきい値-1件のときhintは発火せずキーも無い"""
        activity_id = _make_activity_with_domain_tag()
        topic_id = _make_topic_with_domain_tag()
        for i in range(_RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD - 1):
            add_decision(decision=f"決定{i}", reason="理由", topic_id=topic_id)

        result = check_in(activity_id)

        assert "error" not in result
        assert "hints" not in result

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

        result = check_in(activity_id)

        assert "error" not in result
        assert "hints" in result
        assert any("蓄積しています" in h and DOMAIN_TAG in h for h in result["hints"])

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

        result = check_in(activity_id)

        assert "error" not in result
        assert "hints" not in result

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

        result = check_in(activity_id)

        assert "error" not in result
        assert "hints" in result
        delta_hints = [h for h in result["hints"] if "最終更新以降" in h]
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

        result = check_in(activity_id)

        assert "error" not in result
        assert "hints" not in result

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

        result = check_in(activity_id)

        assert "error" not in result
        assert "hints" not in result

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

        result = check_in(activity_id)

        assert "error" not in result
        assert "hints" not in result, (
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

        result = check_in(activity_id)

        assert "error" not in result
        assert "hints" not in result, (
            "素タグ（namespace空文字）がhint判定対象になっている"
        )

    def test_hints_key_absent_when_no_tag_fires(self, temp_db):
        """どのtagも発火条件を満たさないとき、resultにhintsキーは含まれない"""
        activity_id = _make_activity_with_domain_tag()
        topic_id = _make_topic_with_domain_tag()
        # ブートストラップしきい値未満のdecisionのみ
        add_decision(decision="単一決定", reason="理由", topic_id=topic_id)

        result = check_in(activity_id)

        assert "error" not in result
        assert "hints" not in result
