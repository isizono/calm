"""entity write（add_*/update_*/retract/relation/pin）→ relay outbox の
core内部publish（src.services.relay.entity_publish）の unit test。

relay_publish（セッション向け4動詞、tests/unit/test_relay_service_publish.py）とは
別物で、entity write のcommit直前にoutbox行を1件INSERTするフックを検証する。
"""
import json

import pytest

from src.db import get_connection
from src.services import ask_service as ak
from src.services.activity_service import add_activity, update_activity
from src.services.decision_service import add_decisions
from src.services.discussion_log_service import add_logs
from src.services.habit_service import add_habit, update_habit
from src.services.material_service import add_material, update_material
from src.services.pin_service import add_pin, remove_pin
from src.services.relation_service import add_relation
from src.services.retract_service import retract
from src.services.tag_service import _injected_tags, update_tag
from src.services.topic_service import add_topic

DEFAULT_TAGS = ["domain:test"]


@pytest.fixture(autouse=True)
def relay_connected(tmp_path, monkeypatch):
    """relay接続済み（RELAY_BEARER_TOKEN設定済み）環境をデフォルトにする。

    autouse かつ temp_db に依存しないため、temp_db（init_database() 経由で
    db.py の seed 投入 ensure_tag_ids(conn, [("domain", "default")]) を含む）より
    先に実行される。これにより、"domain:default" タグの event:created が
    outbox に1件紛れ込む（token設定済み環境の副作用として無害・許容）。

    未接続時no-op挙動を検証するTestNoOpWhenRelayNotConfiguredは、この
    fixtureを同名でオーバーライドしtokenを一切設定しない。
    """
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)


@pytest.fixture(autouse=True)
def _clear_injected_tags():
    _injected_tags.clear()


def _outbox_rows() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM relay_outbox ORDER BY id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["labels"] = json.loads(item["labels"])
            result.append(item)
        return result
    finally:
        conn.close()


def _rows_for(ref_type: str, ref_id) -> list[dict]:
    return [r for r in _outbox_rows() if r["ref_type"] == ref_type and r["ref_id"] == str(ref_id)]


class TestNoOpWhenRelayNotConfigured:
    """RELAY_BEARER_TOKEN未設定（relay未接続）環境ではoutboxが積まれない。"""

    @pytest.fixture(autouse=True)
    def relay_connected(self, tmp_path, monkeypatch):
        """モジュールレベルのrelay_connectedを同名でオーバーライドし、tokenを
        一切設定しない（RELAY_STATE_DIRは隔離し、init_database()のseed投入が
        実マシンの ~/.cc-memory/relay/credential.json にフォールバックしない
        ようにする）。autouseかつtemp_dbに依存しないため、init_database()より
        先に実行される。
        """
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
        monkeypatch.delenv("RELAY_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("RELAY_BASE_URL", raising=False)

    def test_add_decisions_is_noop_without_token(self, temp_db, disable_embedding):
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "d1", "reason": "r1"}
        ])
        assert not result["errors"]
        assert _outbox_rows() == []

    def test_add_material_is_noop_without_token(self, temp_db, disable_embedding):
        result = add_material(
            title="m1", content="c1", tags=DEFAULT_TAGS, source="test"
        )
        assert "error" not in result
        assert _outbox_rows() == []


class TestCreatedEventOnRepresentativeWritePaths:
    """代表write経路（add_*）でoutbox行が1件生成され、ref_type/event/entity:が正しいこと。"""

    def test_add_topic_publishes_created(self, temp_db, disable_embedding):
        topic = add_topic(title="トピック", description="説明", tags=DEFAULT_TAGS)
        rows = _rows_for("topic", topic["topic_id"])
        assert len(rows) == 1
        assert "entity:topic" in rows[0]["labels"]
        assert "event:created" in rows[0]["labels"]
        assert "domain:test" in rows[0]["labels"]
        assert rows[0]["title"] == "トピック"

    def test_add_activity_publishes_created(self, temp_db, disable_embedding):
        activity = add_activity(
            title="アクティビティ", description="説明", tags=DEFAULT_TAGS, check_in=False
        )
        rows = _rows_for("activity", activity["activity_id"])
        assert len(rows) == 1
        assert "entity:activity" in rows[0]["labels"]
        assert "event:created" in rows[0]["labels"]

    def test_add_material_publishes_created(self, temp_db, disable_embedding):
        material = add_material(title="資材", content="本文", tags=DEFAULT_TAGS, source="test")
        rows = _rows_for("material", material["material_id"])
        assert len(rows) == 1
        assert "entity:material" in rows[0]["labels"]
        assert "event:created" in rows[0]["labels"]

    def test_add_decisions_publishes_created_with_topic_parent_label(self, temp_db, disable_embedding):
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "d1", "reason": "r1", "tags": DEFAULT_TAGS}
        ])
        decision_id = result["created"][0]["decision_id"]
        rows = _rows_for("decision", decision_id)
        assert len(rows) == 1
        assert "entity:decision" in rows[0]["labels"]
        assert "event:created" in rows[0]["labels"]
        assert f"topic:{topic['topic_id']}" in rows[0]["labels"]
        assert "domain:test" in rows[0]["labels"]

    def test_decision_title_falls_back_to_decision_text(self, temp_db, disable_embedding):
        """titleを省略したdecisionは、A.4.4のCOALESCE(title, decision)通りdecision本文がtitleになる。"""
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "タイトル省略時の本文", "reason": "r1"}
        ])
        decision_id = result["created"][0]["decision_id"]
        rows = _rows_for("decision", decision_id)
        assert rows[0]["title"] == "タイトル省略時の本文"

    def test_add_logs_publishes_created(self, temp_db, disable_embedding):
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        result = add_logs([{"topic_id": topic["topic_id"], "content": "ログ本文"}])
        log_id = result["created"][0]["log_id"]
        rows = _rows_for("log", log_id)
        assert len(rows) == 1
        assert "entity:log" in rows[0]["labels"]
        assert "event:created" in rows[0]["labels"]
        assert f"topic:{topic['topic_id']}" in rows[0]["labels"]

    def test_add_habit_publishes_created(self, temp_db):
        result = add_habit("振る舞い")
        rows = _rows_for("habit", result["habit_id"])
        assert len(rows) == 1
        assert "entity:habit" in rows[0]["labels"]
        assert "event:created" in rows[0]["labels"]
        assert rows[0]["title"] == "振る舞い"

    def test_new_tag_use_publishes_created(self, temp_db, disable_embedding):
        """add_decisionsで初めて使われるタグ文字列は、decisionのcreatedとは別にtagのcreatedも積む。"""
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        add_decisions([
            {
                "topic_id": topic["topic_id"],
                "decision": "d1",
                "reason": "r1",
                "tags": ["glossary:brand-new-tag-xyz"],
            }
        ])
        rows = [r for r in _outbox_rows() if r["ref_type"] == "tag"]
        assert any("event:created" in r["labels"] and "entity:tag" in r["labels"] for r in rows)

    def test_topic_has_no_one_hop_parent_labels(self, temp_db, disable_embedding):
        """topicは階層の最上位で親を持たないため、自身に属する子の数に関わらずlabelsは
        自身のtags + entity:/event:のみ（子をparent-labelとして巻き込まない）。"""
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        add_decisions([
            {"topic_id": topic["topic_id"], "decision": f"d{i}", "reason": "r"}
            for i in range(3)
        ])
        rows = _rows_for("topic", topic["topic_id"])
        assert len(rows) == 1
        assert set(rows[0]["labels"]) == {"entity:topic", "event:created", "domain:test"}


class TestAskSelfLabel:
    """askはentity write時、自身を指すself label（ask:{id}）が付与される。"""

    def test_add_ask_publishes_created_with_self_label(self, temp_db, disable_embedding):
        activity = add_activity(
            title="a", description="d", tags=DEFAULT_TAGS, check_in=False
        )
        result = ak.add_ask("質問", tags=["domain:test"], blocks=[activity["activity_id"]])
        rows = _rows_for("ask", result["id"])
        assert len(rows) == 1
        assert "entity:ask" in rows[0]["labels"]
        assert "event:created" in rows[0]["labels"]
        assert f"ask:{result['id']}" in rows[0]["labels"]

    def test_non_ask_entity_types_do_not_get_ask_self_label(self, temp_db, disable_embedding):
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        material = add_material(title="m", content="c", tags=DEFAULT_TAGS, source="test")
        activity = add_activity(
            title="a", description="d", tags=DEFAULT_TAGS, check_in=False
        )
        for ref_type, ref_id in (
            ("topic", topic["topic_id"]),
            ("material", material["material_id"]),
            ("activity", activity["activity_id"]),
        ):
            rows = _rows_for(ref_type, ref_id)
            assert rows
            assert not any(
                label.startswith("ask:") for row in rows for label in row["labels"]
            )


class TestUpdatedEventOnRepresentativeWritePaths:
    def test_update_material_publishes_updated(self, temp_db, disable_embedding):
        material = add_material(title="資材", content="本文", tags=DEFAULT_TAGS, source="test")
        update_material(material["material_id"], content="更新後本文")
        rows = _rows_for("material", material["material_id"])
        assert len(rows) == 2
        assert "event:created" in rows[0]["labels"]
        assert "event:updated" in rows[1]["labels"]

    def test_update_habit_publishes_updated(self, temp_db):
        habit = add_habit("振る舞い")
        update_habit(habit["habit_id"], content="更新後")
        rows = _rows_for("habit", habit["habit_id"])
        assert len(rows) == 2
        assert "event:updated" in rows[1]["labels"]

    def test_update_tag_publishes_updated(self, temp_db, disable_embedding):
        topic = add_topic(title="t", description="d", tags=["glossary:existing-tag"])
        update_tag("glossary:existing-tag", notes="教訓")
        rows = [r for r in _outbox_rows() if r["ref_type"] == "tag"]
        assert any("event:updated" in r["labels"] for r in rows)


class TestActivityStatusDiffDetection:
    """update_activityはdecision 3076に基づきstatus遷移のみ厳密化する。"""

    def test_status_transition_publishes(self, temp_db, disable_embedding):
        activity = add_activity(
            title="a", description="d", tags=DEFAULT_TAGS, check_in=False
        )
        before = len(_rows_for("activity", activity["activity_id"]))
        update_activity(activity["activity_id"], status="in_progress")
        after = _rows_for("activity", activity["activity_id"])
        assert len(after) == before + 1
        assert "event:updated" in after[-1]["labels"]

    def test_status_no_op_does_not_publish(self, temp_db, disable_embedding):
        activity = add_activity(
            title="a", description="d", tags=DEFAULT_TAGS, check_in=False
        )
        before = len(_rows_for("activity", activity["activity_id"]))
        # activityは作成直後 status="pending"。同じ値を指定 = 実質no-op
        update_activity(activity["activity_id"], status="pending")
        after = _rows_for("activity", activity["activity_id"])
        assert len(after) == before

    def test_non_status_field_publishes_even_without_status_change(self, temp_db, disable_embedding):
        activity = add_activity(
            title="a", description="d", tags=DEFAULT_TAGS, check_in=False
        )
        before = len(_rows_for("activity", activity["activity_id"]))
        update_activity(activity["activity_id"], title="新タイトル")
        after = _rows_for("activity", activity["activity_id"])
        assert len(after) == before + 1


class TestRetract:
    def test_retract_publishes_retracted(self, temp_db, disable_embedding):
        material = add_material(title="資材", content="本文", tags=DEFAULT_TAGS, source="test")
        retract("material", [material["material_id"]])
        rows = _rows_for("material", material["material_id"])
        assert len(rows) == 2
        assert "event:retracted" in rows[-1]["labels"]

    def test_undo_does_not_publish(self, temp_db, disable_embedding):
        material = add_material(title="資材", content="本文", tags=DEFAULT_TAGS, source="test")
        retract("material", [material["material_id"]])
        before = len(_rows_for("material", material["material_id"]))
        retract("material", [material["material_id"]], undo=True)
        after = _rows_for("material", material["material_id"])
        assert len(after) == before

    def test_already_retracted_is_idempotent_no_extra_publish(self, temp_db, disable_embedding):
        material = add_material(title="資材", content="本文", tags=DEFAULT_TAGS, source="test")
        retract("material", [material["material_id"]])
        before = len(_rows_for("material", material["material_id"]))
        retract("material", [material["material_id"]])
        after = _rows_for("material", material["material_id"])
        assert len(after) == before


class TestRelationPublish:
    """add_relation/remove_relationはrelation自体を独立publishせず、source/target
    両方のentityをevent:updatedでpublishする（decision 3065）。"""

    def test_add_relation_publishes_both_endpoints(self, temp_db, disable_embedding):
        material = add_material(title="m", content="c", tags=DEFAULT_TAGS, source="test")
        activity = add_activity(title="a", description="d", tags=DEFAULT_TAGS, check_in=False)

        before_material = len(_rows_for("material", material["material_id"]))
        before_activity = len(_rows_for("activity", activity["activity_id"]))

        result = add_relation(
            "activity", activity["activity_id"],
            [{"type": "material", "ids": [material["material_id"]]}],
        )
        assert result["added"] == 1

        after_material = _rows_for("material", material["material_id"])
        after_activity = _rows_for("activity", activity["activity_id"])
        assert len(after_material) == before_material + 1
        assert len(after_activity) == before_activity + 1
        assert "event:updated" in after_material[-1]["labels"]
        assert "event:updated" in after_activity[-1]["labels"]

    def test_idempotent_add_relation_does_not_republish(self, temp_db, disable_embedding):
        material = add_material(title="m", content="c", tags=DEFAULT_TAGS, source="test")
        activity = add_activity(title="a", description="d", tags=DEFAULT_TAGS, check_in=False)
        add_relation(
            "activity", activity["activity_id"],
            [{"type": "material", "ids": [material["material_id"]]}],
        )
        before = len(_rows_for("material", material["material_id"]))
        result = add_relation(
            "activity", activity["activity_id"],
            [{"type": "material", "ids": [material["material_id"]]}],
        )
        assert result["added"] == 0
        after = len(_rows_for("material", material["material_id"]))
        assert after == before

    def test_relation_to_entity_without_updated_at_column_still_publishes(self, temp_db, disable_embedding):
        """decision/log/topic/tag/habitはupdated_atカラムを持たないため、UPDATE文は
        スキップされるがpublish自体は行われる（暫定の縮退、decision 3065参照）。"""
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        decision_result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "d1", "reason": "r1"}
        ])
        decision_id = decision_result["created"][0]["decision_id"]
        activity = add_activity(title="a", description="d", tags=DEFAULT_TAGS, check_in=False)

        before = len(_rows_for("decision", decision_id))
        result = add_relation(
            "activity", activity["activity_id"],
            [{"type": "decision", "ids": [decision_id]}],
        )
        assert result["added"] == 1
        after = _rows_for("decision", decision_id)
        assert len(after) == before + 1
        assert "event:updated" in after[-1]["labels"]

    def test_related_relation_does_not_add_parent_label(self, temp_db, disable_embedding):
        """relation_type='related'（デフォルト）はどちらの向きにも親帰属を表さないため、
        source/target双方のpublishに相手をparent labelとして混入させない。"""
        material = add_material(title="m", content="c", tags=DEFAULT_TAGS, source="test")
        activity = add_activity(title="a", description="d", tags=DEFAULT_TAGS, check_in=False)

        result = add_relation(
            "activity", activity["activity_id"],
            [{"type": "material", "ids": [material["material_id"]]}],
        )
        assert result["added"] == 1

        activity_rows = _rows_for("activity", activity["activity_id"])
        material_rows = _rows_for("material", material["material_id"])
        assert f"material:{material['material_id']}" not in activity_rows[-1]["labels"]
        assert f"activity:{activity['activity_id']}" not in material_rows[-1]["labels"]

    def test_depends_on_relation_does_not_add_parent_label(self, temp_db, disable_embedding):
        """depends_onはactivity同士の依存関係であり、親帰属ではないため
        依存先をparent labelとして混入させない。"""
        dependency = add_activity(title="dependency", description="d", tags=DEFAULT_TAGS, check_in=False)
        dependent = add_activity(title="dependent", description="d", tags=DEFAULT_TAGS, check_in=False)

        result = add_relation(
            "activity", dependent["activity_id"],
            [{"type": "activity", "ids": [dependency["activity_id"]]}],
            relation_type="depends_on",
        )
        assert result["added"] == 1

        dependent_rows = _rows_for("activity", dependent["activity_id"])
        assert f"activity:{dependency['activity_id']}" not in dependent_rows[-1]["labels"]

    def test_supersedes_relation_does_not_add_parent_label(self, temp_db, disable_embedding):
        """supersedesはdecision同士の置き換え関係であり、親帰属ではないため
        置き換え先(旧decision)をparent labelとして混入させない。"""
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        old = add_decisions([{"topic_id": topic["topic_id"], "decision": "old", "reason": "r"}])
        new = add_decisions([{"topic_id": topic["topic_id"], "decision": "new", "reason": "r"}])
        old_id = old["created"][0]["decision_id"]
        new_id = new["created"][0]["decision_id"]

        result = add_relation(
            "decision", new_id,
            [{"type": "decision", "ids": [old_id]}],
            relation_type="supersedes",
        )
        assert result["added"] == 1

        new_rows = _rows_for("decision", new_id)
        assert f"decision:{old_id}" not in new_rows[-1]["labels"]

    def test_belongs_to_relation_adds_parent_label(self, temp_db, disable_embedding):
        """親帰属パターン（子→topic）はrelation_type='related'指定でも内部でbelongs_toに
        自動格上げされ、この場合のみparent labelが付く。"""
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        material = add_material(title="m", content="c", tags=DEFAULT_TAGS, source="test")

        result = add_relation(
            "material", material["material_id"],
            [{"type": "topic", "ids": [topic["topic_id"]]}],
        )
        assert result["added"] == 1

        material_rows = _rows_for("material", material["material_id"])
        assert f"topic:{topic['topic_id']}" in material_rows[-1]["labels"]


class TestPinPublish:
    def test_add_pin_publishes_both_endpoints(self, temp_db, disable_embedding):
        material = add_material(title="m", content="c", tags=DEFAULT_TAGS, source="test")
        activity = add_activity(title="a", description="d", tags=DEFAULT_TAGS, check_in=False)

        before_material = len(_rows_for("material", material["material_id"]))
        before_activity = len(_rows_for("activity", activity["activity_id"]))

        result = add_pin("activity", activity["activity_id"], "material", material["material_id"])
        assert "error" not in result

        assert len(_rows_for("material", material["material_id"])) == before_material + 1
        assert len(_rows_for("activity", activity["activity_id"])) == before_activity + 1

    def test_remove_pin_publishes_both_endpoints(self, temp_db, disable_embedding):
        material = add_material(title="m", content="c", tags=DEFAULT_TAGS, source="test")
        activity = add_activity(title="a", description="d", tags=DEFAULT_TAGS, check_in=False)
        add_pin("activity", activity["activity_id"], "material", material["material_id"])

        before_material = len(_rows_for("material", material["material_id"]))
        before_activity = len(_rows_for("activity", activity["activity_id"]))

        result = remove_pin("activity", activity["activity_id"], "material", material["material_id"])
        assert result["removed"] == 1

        assert len(_rows_for("material", material["material_id"])) == before_material + 1
        assert len(_rows_for("activity", activity["activity_id"])) == before_activity + 1

    def test_idempotent_remove_pin_does_not_republish(self, temp_db, disable_embedding):
        material = add_material(title="m", content="c", tags=DEFAULT_TAGS, source="test")
        activity = add_activity(title="a", description="d", tags=DEFAULT_TAGS, check_in=False)
        add_pin("activity", activity["activity_id"], "material", material["material_id"])
        remove_pin("activity", activity["activity_id"], "material", material["material_id"])

        before = len(_rows_for("material", material["material_id"]))
        result = remove_pin("activity", activity["activity_id"], "material", material["material_id"])
        assert result["removed"] == 0
        after = len(_rows_for("material", material["material_id"]))
        assert after == before


class TestAddActivityPinsPublishOrder:
    """add_activityのpins引数はevent:createdをpin由来のevent:updatedより必ず先にoutboxへ
    積み、activity自身のbump+publishをpin件数分重複させない（複数targetの一括追加は
    relation_serviceの_bump_and_publish_endpoints_with_connと同じくsource1回+target重複排除）。"""

    def test_created_precedes_updated_and_activity_bump_is_not_duplicated(self, temp_db, disable_embedding):
        material = add_material(title="m", content="c", tags=DEFAULT_TAGS, source="test")
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)

        before_material = len(_rows_for("material", material["material_id"]))
        before_topic = len(_rows_for("topic", topic["topic_id"]))

        result = add_activity(
            title="a", description="d", tags=DEFAULT_TAGS, check_in=False,
            pins=[
                {"type": "material", "ref": material["material_id"]},
                {"type": "topic", "ref": topic["topic_id"]},
            ],
        )
        assert "error" not in result
        activity_id = result["activity_id"]

        activity_rows = _rows_for("activity", activity_id)
        # event:created 1件 + pinsループ後にまとめてbumpされたevent:updated 1件のみ。
        # pin 2件を渡してもactivity側の行が2件（1件/pin）に重複増殖しないことを検証する。
        assert len(activity_rows) == 2
        assert "event:created" in activity_rows[0]["labels"]
        assert "event:updated" in activity_rows[1]["labels"]

        material_rows = _rows_for("material", material["material_id"])
        assert len(material_rows) == before_material + 1
        assert "event:updated" in material_rows[-1]["labels"]

        topic_rows = _rows_for("topic", topic["topic_id"])
        assert len(topic_rows) == before_topic + 1
        assert "event:updated" in topic_rows[-1]["labels"]

        # activityのevent:createdは、pins由来のevent:updated（activity自身のbump分・
        # material・topic）のいずれよりも先にoutboxへ積まれる（idが小さい）
        created_id = activity_rows[0]["id"]
        updated_ids = [activity_rows[1]["id"], material_rows[-1]["id"], topic_rows[-1]["id"]]
        assert all(created_id < updated_id for updated_id in updated_ids)
