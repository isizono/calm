"""write系 MCP tool (経路1) の生ID→cite自動変換の統合テスト。

7 write tool (add_material / update_material / add_logs / add_decisions /
add_topic / add_activity / update_activity) が service 層内で
apply_and_writeback_conversions を経由し、保存時に本文中の生 `X#NNN` を
`{{cite:X#NNN}}` へ自動変換して DB に書き戻し、citation_event_log に
source="write_auto_convert" のイベントを記録することを検証する。
"""
import json
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.activity_service import add_activity, update_activity
from src.services.citations_service import apply_and_writeback_conversions
from src.services.decision_service import add_decisions
from src.services.discussion_log_service import add_logs
from src.services.material_service import add_material, update_material
from src.services.topic_service import add_topic

DEFAULT_TAGS = ["domain:test"]

# 存在しない (dangling) target として使う ID。実在 seed material の id と衝突しない
# よう十分大きい値を使う。
DANGLING_ID = 9999


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def topic_id(temp_db):
    result = add_topic(
        title="Test Topic",
        description="Topic for auto-convert tests",
        tags=DEFAULT_TAGS,
    )
    return result["topic_id"]


def _seed_material(title: str = "tgt", content: str = "body") -> int:
    """seed 用に materials へ 1 行 INSERT して id を返す (conversion を経由しない生 INSERT)。"""
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO materials (title, content, source) VALUES (?, ?, ?)",
                (title, content, "seed"),
            )
        return cur.lastrowid
    finally:
        conn.close()


def _fetch_material(material_id: int) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, title, content FROM materials WHERE id = ?",
            (material_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _fetch_decision(decision_id: int) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, decision, reason FROM decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _fetch_topic(topic_id_: int) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, title, description FROM discussion_topics WHERE id = ?",
            (topic_id_,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _fetch_activity(activity_id: int) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, title, description FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _fetch_log(log_id: int) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, title, content FROM discussion_logs WHERE id = ?",
            (log_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _all_event_rows() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, source, tool_name, target_entity_type, target_entity_id, "
            "target_field, before_text, after_text, verification_result, extra_json "
            "FROM citation_event_log ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _citations_for(owner_type: str, owner_id: int) -> list[tuple]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT target_type, target_id FROM citations "
            "WHERE owner_type = ? AND owner_id = ? ORDER BY occurrence",
            (owner_type, owner_id),
        ).fetchall()
        return [(r["target_type"], r["target_id"]) for r in rows]
    finally:
        conn.close()


# ============================================================
# add_material
# ============================================================
class TestAddMaterialAutoConvert:
    def test_raw_id_converted_and_event_recorded(self, temp_db):
        """生IDを含むcontentでadd_materialを呼ぶと、DB格納本文がcite形式に書き換わり、
        citation_event_logにsource=write_auto_convertのイベントが1件記録される"""
        target_id = _seed_material()
        result = add_material(
            title="t",
            content=f"see M#{target_id}",
            tags=DEFAULT_TAGS,
            source="s",
        )
        material_id = result["material_id"]
        row = _fetch_material(material_id)
        assert row["content"] == f"see {{{{cite:M#{target_id}}}}}"

        events = [e for e in _all_event_rows() if e["target_entity_id"] == material_id]
        assert len(events) == 1
        assert events[0]["source"] == "write_auto_convert"
        assert events[0]["tool_name"] == "add_material"
        assert events[0]["target_field"] == "content"
        assert events[0]["verification_result"] == "exists"

    def test_idempotent_no_change_no_event(self, temp_db):
        """既存cite形式のみを含む本文では、本文が不変でイベントも記録されない"""
        target_id = _seed_material()
        original = f"existing {{{{cite:M#{target_id}}}}} only"
        result = add_material(
            title="t",
            content=original,
            tags=DEFAULT_TAGS,
            source="s",
        )
        material_id = result["material_id"]
        row = _fetch_material(material_id)
        assert row["content"] == original
        events = [e for e in _all_event_rows() if e["target_entity_id"] == material_id]
        assert events == []

    def test_dangling_target_rewritten_to_deleted_marker(self, temp_db):
        """存在しないtarget IDを含む本文では、該当箇所が[deleted ...]マーカーに確定
        書き換えされ、verification_result=danglingで記録される"""
        result = add_material(
            title="t",
            content=f"lost M#{DANGLING_ID} forever",
            tags=DEFAULT_TAGS,
            source="s",
        )
        material_id = result["material_id"]
        row = _fetch_material(material_id)
        assert row["content"] == f"lost [deleted M#{DANGLING_ID}] forever"

        events = [e for e in _all_event_rows() if e["target_entity_id"] == material_id]
        assert len(events) == 1
        assert events[0]["verification_result"] == "dangling"
        extra = json.loads(events[0]["extra_json"])
        assert extra["dangling_targets"] == [{"type": "material", "id": DANGLING_ID}]

    def test_duplicate_raw_id_in_same_field_all_converted(self, temp_db):
        """同一field内に同じ生IDが複数回出現する場合、全出現が変換される"""
        target_id = _seed_material()
        content = f"first M#{target_id}, second M#{target_id}, third M#{target_id}"
        result = add_material(
            title="t",
            content=content,
            tags=DEFAULT_TAGS,
            source="s",
        )
        material_id = result["material_id"]
        row = _fetch_material(material_id)
        cite = f"{{{{cite:M#{target_id}}}}}"
        assert row["content"] == f"first {cite}, second {cite}, third {cite}"

    def test_citations_table_reflects_converted_body(self, temp_db):
        """変換後の本文でcitationsテーブルが再構築される(cite抽出は変換後本文由来)"""
        target_id = _seed_material()
        result = add_material(
            title="t",
            content=f"see M#{target_id}",
            tags=DEFAULT_TAGS,
            source="s",
        )
        material_id = result["material_id"]
        assert _citations_for("material", material_id) == [("material", target_id)]

    def test_embedding_text_matches_converted_content(self, temp_db, monkeypatch):
        """embedding生成テキストがDB格納テキスト(変換後)と一致する"""
        target_id = _seed_material()
        captured = {}

        def spy(entity_type, entity_id, text):
            captured["text"] = text
            return None

        monkeypatch.setattr(
            "src.services.material_service.generate_and_store_embedding", spy
        )
        result = add_material(
            title="t",
            content=f"see M#{target_id}",
            tags=DEFAULT_TAGS,
            source="s",
        )
        material_id = result["material_id"]
        row = _fetch_material(material_id)
        assert row["content"] == f"see {{{{cite:M#{target_id}}}}}"
        assert row["content"] in captured["text"]
        # 変換前の生ID表記は embedding テキストに残っていない
        assert f"M#{target_id}" not in captured["text"].replace(row["content"], "")

    def test_self_reference_treated_as_existing(self, temp_db):
        """INSERT直後の自material IDをcontent内で参照した場合、同一トランザクション内
        SELECTでexists扱いになる(dangling判定にならない)"""
        conn = get_connection()
        next_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM materials"
        ).fetchone()["next_id"]
        conn.close()

        result = add_material(
            title="self ref",
            content=f"refers to M#{next_id} (itself)",
            tags=DEFAULT_TAGS,
            source="s",
        )
        material_id = result["material_id"]
        assert material_id == next_id
        row = _fetch_material(material_id)
        assert row["content"] == f"refers to {{{{cite:M#{material_id}}}}} (itself)"

        events = [e for e in _all_event_rows() if e["target_entity_id"] == material_id]
        assert len(events) == 1
        assert events[0]["verification_result"] == "exists"


# ============================================================
# update_material
# ============================================================
class TestUpdateMaterialAutoConvert:
    def test_content_converted_title_untouched_when_unspecified(self, temp_db):
        """update_materialでcontentだけ渡した場合、contentは変換されるが、未指定の
        titleは既存値に生IDが含まれていても書き換わらない"""
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO materials (title, content, source) VALUES (?, ?, ?)",
            (f"legacy M#{DANGLING_ID} title", "old content", "seed"),
        )
        material_id = cur.lastrowid
        conn.commit()
        conn.close()

        target_id = _seed_material()
        result = update_material(
            material_id=material_id,
            content=f"updated with M#{target_id}",
        )
        assert "error" not in result
        row = _fetch_material(material_id)
        assert row["content"] == f"updated with {{{{cite:M#{target_id}}}}}"
        assert row["title"] == f"legacy M#{DANGLING_ID} title"

        events = [e for e in _all_event_rows() if e["target_entity_id"] == material_id]
        assert len(events) == 1
        assert events[0]["target_field"] == "content"

    def test_citations_rebuilt_even_when_body_unchanged(self, temp_db):
        """update_materialの「本文無変更でもcitations全再構築」の既存挙動が維持される"""
        target_id = _seed_material()
        create = add_material(
            title="t",
            content=f"see {{{{cite:M#{target_id}}}}}",
            tags=DEFAULT_TAGS,
            source="s",
        )
        material_id = create["material_id"]

        conn = get_connection()
        conn.execute(
            "DELETE FROM citations WHERE owner_type = 'material' AND owner_id = ?",
            (material_id,),
        )
        conn.commit()
        conn.close()
        assert _citations_for("material", material_id) == []

        result = update_material(material_id=material_id, tags=["domain:test", "design"])
        assert "error" not in result
        assert _citations_for("material", material_id) == [("material", target_id)]

    def test_embedding_text_matches_converted_content_after_update(
        self, temp_db, monkeypatch
    ):
        """update_material後のembedding生成テキストも変換後DB内容と一致する"""
        material_id = _seed_material(title="t", content="old")
        target_id = _seed_material()
        captured = {}

        def spy(entity_type, entity_id, text):
            captured["text"] = text
            return None

        monkeypatch.setattr(
            "src.services.material_service.generate_and_store_embedding", spy
        )
        result = update_material(
            material_id=material_id,
            content=f"updated M#{target_id}",
        )
        assert "error" not in result
        row = _fetch_material(material_id)
        assert row["content"] == f"updated {{{{cite:M#{target_id}}}}}"
        assert row["content"] in captured["text"]


# ============================================================
# add_logs
# ============================================================
class TestAddLogsAutoConvert:
    def test_content_converted_title_keeps_raw_id(self, temp_db, topic_id):
        """add_logsでcontentはcite化され、auto生成titleには生IDが残る
        (titleは非パース対象、現状維持方針の意図確認テスト)"""
        target_id = _seed_material()
        content = f"M#{target_id} についての議論"
        result = add_logs([{"topic_id": topic_id, "content": content}])
        assert result["errors"] == []
        log_id = result["created"][0]["log_id"]

        row = _fetch_log(log_id)
        assert row["content"] == f"{{{{cite:M#{target_id}}}}} についての議論"
        # title は content の先頭行から auto-generate されるが、非パース対象なので
        # 生ID表記のまま残る
        assert f"M#{target_id}" in row["title"]

        events = [e for e in _all_event_rows() if e["target_entity_id"] == log_id]
        assert len(events) == 1
        assert events[0]["target_field"] == "content"

    def test_batch_partial_failure_savepoint_rollback_others_preserved(
        self, temp_db, topic_id
    ):
        """add_logsバッチで1 itemがIntegrityErrorになった場合、該当itemの変換書き戻し
        とイベントはSAVEPOINTごと巻き戻り、他itemの変換と記録は残る"""
        target_id = _seed_material()
        bad_topic_id = topic_id + DANGLING_ID
        result = add_logs(
            [
                {"topic_id": topic_id, "content": f"first M#{target_id}"},
                {"topic_id": bad_topic_id, "content": "this item fails"},
                {"topic_id": topic_id, "content": f"third M#{target_id}"},
            ]
        )
        assert len(result["created"]) == 2
        assert len(result["errors"]) == 1
        assert result["errors"][0]["index"] == 1

        events = _all_event_rows()
        assert len(events) == 2
        after_texts = {e["after_text"] for e in events}
        assert f"first {{{{cite:M#{target_id}}}}}" in after_texts
        assert f"third {{{{cite:M#{target_id}}}}}" in after_texts


# ============================================================
# add_decisions
# ============================================================
class TestAddDecisionsAutoConvert:
    def test_decision_and_reason_independently_converted(self, temp_db, topic_id):
        """add_decisionsのdecision/reasonは独立に変換される"""
        target_id = _seed_material()
        result = add_decisions(
            [
                {
                    "topic_id": topic_id,
                    "decision": f"adopt M#{target_id}",
                    "reason": f"because of M#{target_id}",
                }
            ]
        )
        assert result["errors"] == []
        decision_id = result["created"][0]["decision_id"]

        row = _fetch_decision(decision_id)
        cite = f"{{{{cite:M#{target_id}}}}}"
        assert row["decision"] == f"adopt {cite}"
        assert row["reason"] == f"because of {cite}"

        events = [e for e in _all_event_rows() if e["target_entity_id"] == decision_id]
        assert len(events) == 2
        assert {e["target_field"] for e in events} == {"decision", "reason"}

    def test_only_field_with_raw_id_gets_event(self, temp_db, topic_id):
        """片方のみ生IDを含む場合は片方のみイベント記録される"""
        target_id = _seed_material()
        result = add_decisions(
            [
                {
                    "topic_id": topic_id,
                    "decision": f"adopt M#{target_id}",
                    "reason": "plain reason with no reference",
                }
            ]
        )
        decision_id = result["created"][0]["decision_id"]
        row = _fetch_decision(decision_id)
        assert row["decision"] == f"adopt {{{{cite:M#{target_id}}}}}"
        assert row["reason"] == "plain reason with no reference"

        events = [e for e in _all_event_rows() if e["target_entity_id"] == decision_id]
        assert len(events) == 1
        assert events[0]["target_field"] == "decision"


# ============================================================
# add_topic
# ============================================================
class TestAddTopicAutoConvert:
    def test_title_and_description_converted(self, temp_db):
        """add_topicのtitle/description両フィールドが変換される"""
        target_id = _seed_material()
        result = add_topic(
            title=f"M#{target_id} discussion",
            description=f"explore M#{target_id}",
            tags=DEFAULT_TAGS,
        )
        new_topic_id = result["topic_id"]
        row = _fetch_topic(new_topic_id)
        cite = f"{{{{cite:M#{target_id}}}}}"
        assert row["title"] == f"{cite} discussion"
        assert row["description"] == f"explore {cite}"

        events = [e for e in _all_event_rows() if e["target_entity_id"] == new_topic_id]
        assert len(events) == 2
        assert {e["target_field"] for e in events} == {"title", "description"}


# ============================================================
# add_activity / update_activity
# ============================================================
class TestAddActivityAutoConvert:
    def test_title_and_description_converted(self, temp_db):
        """add_activityのtitle/description両フィールドが変換される"""
        target_id = _seed_material()
        result = add_activity(
            title=f"M#{target_id} work",
            description=f"plan around M#{target_id}",
            tags=DEFAULT_TAGS,
            check_in=False,
        )
        activity_id = result["activity_id"]
        row = _fetch_activity(activity_id)
        cite = f"{{{{cite:M#{target_id}}}}}"
        assert row["title"] == f"{cite} work"
        assert row["description"] == f"plan around {cite}"

        events = [e for e in _all_event_rows() if e["target_entity_id"] == activity_id]
        assert len(events) == 2

    def test_existing_cite_and_new_raw_mixed_single_event(self, temp_db):
        """descriptionに既存citeと生ID(存在するtarget)が混在する場合、変換後は両方
        cite形式で既存分は再変換されず、eventは1件(description field全体で1event)"""
        existing_target = _seed_material()
        new_target = _seed_material()
        description = (
            f"see {{{{cite:M#{existing_target}}}}} and also M#{new_target}"
        )
        result = add_activity(
            title="act",
            description=description,
            tags=DEFAULT_TAGS,
            check_in=False,
        )
        activity_id = result["activity_id"]
        row = _fetch_activity(activity_id)
        assert row["description"] == (
            f"see {{{{cite:M#{existing_target}}}}} and also {{{{cite:M#{new_target}}}}}"
        )

        events = [
            e
            for e in _all_event_rows()
            if e["target_entity_id"] == activity_id and e["target_field"] == "description"
        ]
        assert len(events) == 1

    def test_related_target_body_not_touched(self, temp_db):
        """related経由のcross-referenceが発生しても、変換対象は自entityの本文のみで、
        related先(material)の本文は書き換わらない(再入なし)"""
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO materials (title, content, source) VALUES (?, ?, ?)",
            ("legacy", f"legacy raw M#{DANGLING_ID} untouched", "seed"),
        )
        material_id = cur.lastrowid
        conn.commit()
        conn.close()

        result = add_activity(
            title="act",
            description=f"relates to M#{material_id}",
            tags=DEFAULT_TAGS,
            related=[{"type": "material", "ids": [material_id]}],
            check_in=False,
        )
        assert "error" not in result
        row = _fetch_material(material_id)
        assert row["content"] == f"legacy raw M#{DANGLING_ID} untouched"


class TestUpdateActivityAutoConvert:
    def test_description_converted_title_untouched_when_unspecified(self, temp_db):
        """update_activityでdescriptionだけ渡した場合、descriptionは変換されるが未指定の
        titleは書き換わらない(update_materialと同型のpartial field素通し)"""
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO activities (title, description, status, orch_managed) "
            "VALUES (?, ?, ?, ?)",
            (f"legacy M#{DANGLING_ID} title", "old description", "pending", 0),
        )
        activity_id = cur.lastrowid
        conn.commit()
        conn.close()

        target_id = _seed_material()
        result = update_activity(
            activity_id=activity_id,
            description=f"updated with M#{target_id}",
        )
        assert "error" not in result
        row = _fetch_activity(activity_id)
        assert row["description"] == f"updated with {{{{cite:M#{target_id}}}}}"
        assert row["title"] == f"legacy M#{DANGLING_ID} title"


# ============================================================
# add_activity — relay outboxへ流出するtitleがconversion後の値であること
# ============================================================
class TestAddActivityRelayOutboxTitleConversion:
    """add_activityがrelay outboxへpublishするtitleが、生ID表記ではなく変換後の
    {{cite:X#NNN}}形式になっていることを検証する。

    publish_entity_event_with_conn は呼び出し時点でDBからtitleを都度SELECTする
    ため、conversion(apply_and_writeback_conversions)がpublishより前に完了して
    いなければ、outboxのtitleに生ID表記が漏れる(add_activityで過去に発生していた
    順序バグの回帰テスト)。"""

    @pytest.fixture(autouse=True)
    def relay_connected(self, tmp_path, monkeypatch):
        """relay接続済み(RELAY_BEARER_TOKEN設定済み)環境にする。

        tests/unit/test_relay_entity_publish.py の relay_connected と同型。
        RELAY_STATE_DIR を隔離し、実マシンのcredential.jsonへフォールバック
        しないようにする。"""
        monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
        monkeypatch.delenv("RELAY_BASE_URL", raising=False)

    def _outbox_rows_for_activity(self, activity_id: int) -> list[dict]:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM relay_outbox WHERE ref_type = 'activity' AND ref_id = ? "
                "ORDER BY id",
                (str(activity_id),),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["labels"] = json.loads(item["labels"])
                result.append(item)
            return result
        finally:
            conn.close()

    def test_created_event_title_is_converted_not_raw(self, temp_db, disable_embedding):
        """titleに生IDを含めてadd_activityしても、outboxのevent:created行のtitleは
        変換後の{{cite:X#NNN}}形式になり、生ID表記(X#NNN)は残らない"""
        target_id = _seed_material()
        result = add_activity(
            title=f"M#{target_id} work",
            description="d",
            tags=DEFAULT_TAGS,
            check_in=False,
        )
        activity_id = result["activity_id"]
        rows = self._outbox_rows_for_activity(activity_id)
        created_rows = [r for r in rows if "event:created" in r["labels"]]
        assert len(created_rows) == 1
        expected_title = f"{{{{cite:M#{target_id}}}}} work"
        assert created_rows[0]["title"] == expected_title
        assert f"M#{target_id}" not in created_rows[0]["title"].replace(expected_title, "")

    def test_pins_bump_updated_event_title_is_also_converted(self, temp_db, disable_embedding):
        """pins指定時にpinsループ後まとめてbumpされるactivity自身のevent:updated行の
        titleも、conversion後の値になっている(pinsブロックがconversionより後に実行
        されても、DBには既にconversion済みの値が入っている必要がある)"""
        target_id = _seed_material()
        pin_target = _seed_material(title="pin target")
        result = add_activity(
            title=f"M#{target_id} work",
            description="d",
            tags=DEFAULT_TAGS,
            check_in=False,
            pins=[{"type": "material", "ref": pin_target}],
        )
        activity_id = result["activity_id"]
        rows = self._outbox_rows_for_activity(activity_id)
        updated_rows = [r for r in rows if "event:updated" in r["labels"]]
        assert len(updated_rows) == 1
        assert updated_rows[0]["title"] == f"{{{{cite:M#{target_id}}}}} work"


# ============================================================
# トランザクション境界
# ============================================================
class TestTransactionAtomicity:
    def test_invalid_entity_type_rolls_back_whole_transaction(self, temp_db):
        """entity_type不正でValueErrorが上がったとき、直前のINSERTを含めてtrans全体が
        中断される(呼び出し元のtry/except/rollback構造の維持を、共有connトランザクション
        で検証する)"""
        conn = get_connection()
        try:
            with pytest.raises(ValueError):
                with conn:
                    conn.execute(
                        "INSERT INTO materials (title, content, source) VALUES (?, ?, ?)",
                        ("t", "c", "s"),
                    )
                    apply_and_writeback_conversions(
                        conn,
                        entity_type="not_a_real_entity_type",
                        entity_id=1,
                        fields_payload={"content": "x"},
                        tool_name="add_material",
                        table="materials",
                    )
        finally:
            conn.close()

        conn2 = get_connection()
        try:
            count = conn2.execute(
                "SELECT COUNT(*) AS c FROM materials"
            ).fetchone()["c"]
        finally:
            conn2.close()
        assert count == 0
