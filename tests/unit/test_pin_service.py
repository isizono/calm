"""pin_service の単体テスト

pinsテーブルへの有向関係（source → target）の追加・削除と
バリデーションエラー・エッジケースをカバーする。
"""
import os
import tempfile
import pytest

from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.discussion_log_service import add_logs
from src.services.decision_service import add_decisions
from src.services.material_service import add_material
from src.services.activity_service import add_activity
from src.services.pin_service import add_pin, remove_pin
from src.services.tag_service import _injected_tags, ensure_tag_ids


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def topic(temp_db):
    """テスト用トピックを作成する"""
    return add_topic(title="テストトピック", description="テスト用", tags=DEFAULT_TAGS)


@pytest.fixture
def activity(temp_db):
    """テスト用アクティビティを作成する"""
    return add_activity(title="テストアクティビティ", description="テスト用", tags=DEFAULT_TAGS)


@pytest.fixture
def material(temp_db):
    """テスト用資材を作成する"""
    return add_material(
        title="テスト資材",
        content="テスト資材の内容",
        tags=DEFAULT_TAGS,
        source="テスト用データ",
    )


class TestAddPinBasic:
    """add_pin の基本動作"""

    def test_add_pin_activity_to_material(self, activity, material):
        """activityからmaterialへのpinを追加できる"""
        activity_id = activity["activity_id"]
        material_id = material["material_id"]

        result = add_pin("activity", activity_id, "material", material_id)

        assert "error" not in result
        assert result["source_type"] == "activity"
        assert result["source_id"] == activity_id
        assert result["target_type"] == "material"
        assert result["target_id"] == material_id

        # DB上にpinsレコードが作成されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM pins WHERE source_type=? AND source_id=? AND target_type=? AND target_id=?",
                ("activity", activity_id, "material", material_id),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_add_pin_topic_to_decision(self, topic):
        """topicからdecisionへのpinを追加できる"""
        topic_id = topic["topic_id"]
        result = add_decisions([
            {"topic_id": topic_id, "decision": "テスト決定", "reason": "テスト理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        pin_result = add_pin("topic", topic_id, "decision", decision_id)

        assert "error" not in pin_result
        assert pin_result["source_type"] == "topic"
        assert pin_result["source_id"] == topic_id
        assert pin_result["target_type"] == "decision"
        assert pin_result["target_id"] == decision_id

    def test_add_pin_topic_to_log(self, topic):
        """topicからlogへのpinを追加できる"""
        topic_id = topic["topic_id"]
        result = add_logs([
            {"topic_id": topic_id, "content": "テストログ内容", "title": "テストログ"},
        ])
        log_id = result["created"][0]["log_id"]

        pin_result = add_pin("topic", topic_id, "log", log_id)

        assert "error" not in pin_result
        assert pin_result["target_type"] == "log"
        assert pin_result["target_id"] == log_id


class TestAddPinTagRef:
    """add_pin の tag ref 解決"""

    def test_add_pin_with_tag_ref_string(self, activity, temp_db):
        """tagのrefをnamespace:name形式の文字列で指定できる"""
        activity_id = activity["activity_id"]

        # tagを作成する
        conn = get_connection()
        try:
            tag_ids = ensure_tag_ids(conn, [("domain", "calm")])
            conn.commit()
            tag_id = tag_ids[0]
        finally:
            conn.close()

        result = add_pin("tag", "domain:calm", "activity", activity_id)

        assert "error" not in result
        assert result["source_type"] == "tag"
        assert result["source_id"] == tag_id
        assert result["target_type"] == "activity"
        assert result["target_id"] == activity_id

    def test_add_pin_with_tag_ref_int(self, activity, temp_db):
        """tagのrefをIDの整数で指定できる"""
        activity_id = activity["activity_id"]

        conn = get_connection()
        try:
            tag_ids = ensure_tag_ids(conn, [("domain", "calm")])
            conn.commit()
            tag_id = tag_ids[0]
        finally:
            conn.close()

        result = add_pin("tag", tag_id, "activity", activity_id)

        assert "error" not in result
        assert result["source_type"] == "tag"
        assert result["source_id"] == tag_id

    def test_add_pin_with_nonexistent_tag_string(self, activity, temp_db):
        """存在しないtag名文字列でNOT_FOUNDエラーを返す"""
        activity_id = activity["activity_id"]

        result = add_pin("tag", "domain:nonexistent", "activity", activity_id)

        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"
        assert "domain:nonexistent" in result["error"]["message"]


class TestAddPinIdempotent:
    """add_pin の冪等性"""

    def test_add_pin_duplicate_is_idempotent(self, activity, material):
        """同じpinを2回追加してもエラーにならない"""
        activity_id = activity["activity_id"]
        material_id = material["material_id"]

        result1 = add_pin("activity", activity_id, "material", material_id)
        result2 = add_pin("activity", activity_id, "material", material_id)

        assert "error" not in result1
        assert "error" not in result2

        # DB上のpinsレコードは1件のみ
        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM pins WHERE source_type=? AND source_id=? AND target_type=? AND target_id=?",
                ("activity", activity_id, "material", material_id),
            ).fetchone()[0]
            assert count == 1
        finally:
            conn.close()


class TestAddPinSelfReference:
    """add_pin の自己参照拒否"""

    def test_add_pin_self_reference_rejected(self, activity):
        """同一エンティティへのself-referenceはVALIDATION_ERRORを返す"""
        activity_id = activity["activity_id"]

        result = add_pin("activity", activity_id, "activity", activity_id)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "Self-reference" in result["error"]["message"]

    def test_add_pin_different_types_allowed(self, topic, material):
        """source_typeとtarget_typeが異なれば同じIDでもpinできる"""
        topic_id = topic["topic_id"]

        # materialのIDがtopic_idと一致するとは限らないが、
        # topic → material は source_type != target_type なので自己参照ではない
        material_id = material["material_id"]

        # ID値が同じになるケースを人工的に作るのは困難なため、
        # 異なる種別の組み合わせが通ることを確認するに留める
        result = add_pin("topic", topic_id, "material", material_id)
        assert "error" not in result


class TestAddPinNotFound:
    """add_pin の存在性チェック"""

    def test_add_pin_nonexistent_source_returns_not_found(self, material):
        """存在しないsource IDでNOT_FOUNDエラーを返す"""
        material_id = material["material_id"]

        result = add_pin("activity", 99999, "material", material_id)

        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"
        assert "activity" in result["error"]["message"]
        assert "99999" in result["error"]["message"]

    def test_add_pin_nonexistent_target_returns_not_found(self, activity):
        """存在しないtarget IDでNOT_FOUNDエラーを返す"""
        activity_id = activity["activity_id"]

        result = add_pin("activity", activity_id, "material", 99999)

        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"
        assert "material" in result["error"]["message"]
        assert "99999" in result["error"]["message"]


class TestAddPinValidationErrors:
    """add_pin のバリデーションエラー"""

    def test_invalid_source_type(self, material):
        """不正なsource_typeでVALIDATION_ERRORを返す"""
        material_id = material["material_id"]

        result = add_pin("invalid_type", 1, "material", material_id)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "source_type" in result["error"]["message"]

    def test_invalid_target_type(self, activity):
        """不正なtarget_typeでVALIDATION_ERRORを返す"""
        activity_id = activity["activity_id"]

        result = add_pin("activity", activity_id, "invalid_type", 1)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "target_type" in result["error"]["message"]

    def test_non_integer_ref_for_non_tag_type(self, activity):
        """tag以外でstr形式のrefを指定するとVALIDATION_ERRORを返す"""
        activity_id = activity["activity_id"]

        result = add_pin("activity", "not_an_int", "activity", activity_id)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestRemovePin:
    """remove_pin の動作"""

    def test_remove_existing_pin(self, activity, material):
        """追加済みpinを削除できる"""
        activity_id = activity["activity_id"]
        material_id = material["material_id"]

        add_pin("activity", activity_id, "material", material_id)
        result = remove_pin("activity", activity_id, "material", material_id)

        assert "error" not in result
        assert result["removed"] == 1

        # DB上のpinsレコードが削除されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM pins WHERE source_type=? AND source_id=? AND target_type=? AND target_id=?",
                ("activity", activity_id, "material", material_id),
            ).fetchone()
            assert row is None
        finally:
            conn.close()

    def test_remove_nonexistent_pin_returns_zero(self, activity, material):
        """存在しないpinの削除はremoved=0を返す（エラーにならない）"""
        activity_id = activity["activity_id"]
        material_id = material["material_id"]

        result = remove_pin("activity", activity_id, "material", material_id)

        assert "error" not in result
        assert result["removed"] == 0

    def test_remove_pin_with_tag_ref_string(self, activity, temp_db):
        """tagのrefをnamespace:name形式の文字列で削除できる"""
        activity_id = activity["activity_id"]

        conn = get_connection()
        try:
            tag_ids = ensure_tag_ids(conn, [("domain", "calm")])
            conn.commit()
            tag_id = tag_ids[0]
        finally:
            conn.close()

        add_pin("tag", "domain:calm", "activity", activity_id)
        result = remove_pin("tag", "domain:calm", "activity", activity_id)

        assert "error" not in result
        assert result["removed"] == 1

    def test_remove_pin_invalid_source_type(self, material):
        """不正なsource_typeでVALIDATION_ERRORを返す"""
        material_id = material["material_id"]

        result = remove_pin("invalid_type", 1, "material", material_id)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_remove_pin_nonexistent_tag_string_is_idempotent(self, activity, temp_db):
        """存在しないtag名文字列で remove しても {"removed": 0} を返し冪等になる"""
        activity_id = activity["activity_id"]

        result = remove_pin("tag", "domain:nonexistent", "activity", activity_id)

        assert result == {"removed": 0}


class TestAddPinSupersededHint:
    """add_pin: supersededなdecisionへのpinはhintを返す"""

    def _setup_superseded(self, topic_id: int) -> tuple[int, int]:
        """topic_id 配下に d_old, d_new を作り、d_new supersedes d_old の関係を張る"""
        from src.services.relation_service import add_relation

        d_old_res = add_decisions([
            {"topic_id": topic_id, "decision": "古い決定", "reason": "旧"},
        ])
        d_new_res = add_decisions([
            {"topic_id": topic_id, "decision": "新しい決定", "reason": "新"},
        ])
        d_old = d_old_res["created"][0]["decision_id"]
        d_new = d_new_res["created"][0]["decision_id"]
        add_relation(
            "decision", d_new, [{"type": "decision", "ids": [d_old]}],
            relation_type="supersedes",
        )
        return d_old, d_new

    def test_pin_to_superseded_target_returns_hint(self, topic, activity):
        """add_pinのtargetがsuperseded decisionの場合hintキーが付き、pin自体は張られる"""
        topic_id = topic["topic_id"]
        activity_id = activity["activity_id"]
        d_old, d_new = self._setup_superseded(topic_id)

        result = add_pin("activity", activity_id, "decision", d_old)

        assert "error" not in result
        assert result["source_type"] == "activity"
        assert result["target_id"] == d_old
        assert "hint" in result
        assert f"decision#{d_old}" in result["hint"]
        assert f"decision#{d_new}" in result["hint"]

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM pins WHERE source_type='activity' AND source_id=? "
                "AND target_type='decision' AND target_id=?",
                (activity_id, d_old),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_pin_to_superseded_source_returns_hint(self, topic, material):
        """add_pinのsourceがsuperseded decisionの場合hintキーが付く"""
        topic_id = topic["topic_id"]
        material_id = material["material_id"]
        d_old, _d_new = self._setup_superseded(topic_id)

        result = add_pin("decision", d_old, "material", material_id)

        assert "error" not in result
        assert "hint" in result

    def test_pin_to_non_superseded_decision_no_hint(self, topic, activity):
        """通常のdecisionへのpinはhintキーが付かない"""
        topic_id = topic["topic_id"]
        activity_id = activity["activity_id"]
        d_res = add_decisions([
            {"topic_id": topic_id, "decision": "通常", "reason": "r"},
        ])
        decision_id = d_res["created"][0]["decision_id"]

        result = add_pin("activity", activity_id, "decision", decision_id)

        assert "error" not in result
        assert "hint" not in result

    def test_pin_to_non_decision_target_no_hint(self, topic, activity, material):
        """target_typeがdecision以外の場合hintキーは付かない"""
        activity_id = activity["activity_id"]
        material_id = material["material_id"]

        result = add_pin("activity", activity_id, "material", material_id)

        assert "error" not in result
        assert "hint" not in result

    def test_pin_both_sides_superseded_returns_two_hints(self, topic):
        """source/target両方がsuperseded decisionの場合hintが2件結合される"""
        topic_id = topic["topic_id"]
        d_src_old, d_src_new = self._setup_superseded(topic_id)
        d_tgt_old, d_tgt_new = self._setup_superseded(topic_id)

        result = add_pin("decision", d_src_old, "decision", d_tgt_old)

        assert "error" not in result
        assert "hint" in result
        hint_lines = result["hint"].split("\n")
        assert len(hint_lines) == 2
        assert any(f"decision#{d_src_old}" in line and f"decision#{d_src_new}" in line for line in hint_lines)
        assert any(f"decision#{d_tgt_old}" in line and f"decision#{d_tgt_new}" in line for line in hint_lines)


class TestTransferPinsWithConn:
    """_transfer_pins_with_conn 単体テスト"""

    def _create_decisions(self, topic_id: int) -> tuple[int, int]:
        d1 = add_decisions([{"topic_id": topic_id, "decision": "d1", "reason": "r"}])
        d2 = add_decisions([{"topic_id": topic_id, "decision": "d2", "reason": "r"}])
        return d1["created"][0]["decision_id"], d2["created"][0]["decision_id"]

    def test_no_pins_returns_zero(self, topic):
        """旧entityにpinが無い場合は0を返す"""
        from src.services.pin_service import _transfer_pins_with_conn

        topic_id = topic["topic_id"]
        d_old, d_new = self._create_decisions(topic_id)

        conn = get_connection()
        try:
            n = _transfer_pins_with_conn(conn, "decision", d_old, d_new)
            conn.commit()
        finally:
            conn.close()
        assert n == 0

    def test_target_side_only(self, topic, activity):
        """target側のpinだけを移動"""
        from src.services.pin_service import _transfer_pins_with_conn

        topic_id = topic["topic_id"]
        activity_id = activity["activity_id"]
        d_old, d_new = self._create_decisions(topic_id)

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO pins (source_type, source_id, target_type, target_id) "
                "VALUES ('activity', ?, 'decision', ?)",
                (activity_id, d_old),
            )
            conn.commit()
            n = _transfer_pins_with_conn(conn, "decision", d_old, d_new)
            conn.commit()
        finally:
            conn.close()
        assert n == 1

    def test_conflict_returns_zero_inserts(self, topic, activity):
        """new側に既存pinがある場合、OR IGNOREで実INSERTは0、旧行は消滅"""
        from src.services.pin_service import _transfer_pins_with_conn

        topic_id = topic["topic_id"]
        activity_id = activity["activity_id"]
        d_old, d_new = self._create_decisions(topic_id)

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO pins (source_type, source_id, target_type, target_id) "
                "VALUES ('activity', ?, 'decision', ?)",
                (activity_id, d_old),
            )
            conn.execute(
                "INSERT INTO pins (source_type, source_id, target_type, target_id) "
                "VALUES ('activity', ?, 'decision', ?)",
                (activity_id, d_new),
            )
            conn.commit()
            n = _transfer_pins_with_conn(conn, "decision", d_old, d_new)
            conn.commit()

            old_count = conn.execute(
                "SELECT COUNT(*) AS c FROM pins WHERE target_type='decision' AND target_id=?",
                (d_old,),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert n == 0
        assert old_count == 0


class TestAddPinWithConnExtraction:
    """_add_pin_with_conn: add_pinから抽出したconn共有版が呼び出し元のトランザクションに参加すること"""

    def test_participates_in_caller_transaction_uncommitted_invisible(self, activity, material):
        """呼び出し元がcommitするまで、他connからpinは見えない"""
        from src.services.pin_service import _add_pin_with_conn

        activity_id = activity["activity_id"]
        material_id = material["material_id"]

        conn = get_connection()
        try:
            result = _add_pin_with_conn(conn, "activity", activity_id, "material", material_id)
            assert "error" not in result
            assert result["target_type"] == "material"
            assert result["target_id"] == material_id

            other_conn = get_connection()
            try:
                row = other_conn.execute(
                    "SELECT * FROM pins WHERE source_type='activity' AND source_id=? "
                    "AND target_type='material' AND target_id=?",
                    (activity_id, material_id),
                ).fetchone()
                assert row is None
            finally:
                other_conn.close()

            conn.commit()

            row = conn.execute(
                "SELECT * FROM pins WHERE source_type='activity' AND source_id=? "
                "AND target_type='material' AND target_id=?",
                (activity_id, material_id),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_error_result_lets_caller_rollback_without_partial_write(self, activity, material):
        """存在しないtargetの場合はエラーdictを返し、呼び出し元のrollbackで何も残らない"""
        from src.services.pin_service import _add_pin_with_conn

        activity_id = activity["activity_id"]
        material_id = material["material_id"]

        conn = get_connection()
        try:
            # 先に無関係のpinをINSERT（同一トランザクション内、まだcommitしていない）
            conn.execute(
                "INSERT INTO pins (source_type, source_id, target_type, target_id) "
                "VALUES ('activity', ?, 'material', ?)",
                (activity_id, material_id),
            )
            result = _add_pin_with_conn(conn, "activity", activity_id, "material", 99999)
            assert "error" in result
            assert result["error"]["code"] == "NOT_FOUND"
            conn.rollback()
        finally:
            conn.close()

        # rollbackにより、事前INSERT分もまとめて消えている
        verify_conn = get_connection()
        try:
            row = verify_conn.execute(
                "SELECT * FROM pins WHERE source_type='activity' AND source_id=? "
                "AND target_type='material' AND target_id=?",
                (activity_id, material_id),
            ).fetchone()
            assert row is None
        finally:
            verify_conn.close()

    def test_still_computes_superseded_hint_when_shared_conn(self, topic, activity):
        """conn共有呼び出しでもsupersededなdecisionへのhint付与は動く"""
        from src.services.pin_service import _add_pin_with_conn
        from src.services.relation_service import add_relation

        topic_id = topic["topic_id"]
        activity_id = activity["activity_id"]

        d_old_res = add_decisions([{"topic_id": topic_id, "decision": "古い決定", "reason": "旧"}])
        d_new_res = add_decisions([{"topic_id": topic_id, "decision": "新しい決定", "reason": "新"}])
        d_old = d_old_res["created"][0]["decision_id"]
        d_new = d_new_res["created"][0]["decision_id"]
        add_relation(
            "decision", d_new, [{"type": "decision", "ids": [d_old]}],
            relation_type="supersedes",
        )

        conn = get_connection()
        try:
            result = _add_pin_with_conn(conn, "activity", activity_id, "decision", d_old)
            conn.commit()
        finally:
            conn.close()

        assert "error" not in result
        assert "hint" in result
        assert f"decision#{d_old}" in result["hint"]
        assert f"decision#{d_new}" in result["hint"]


class TestPinsCascadeDelete:
    """migration 0038: 各entityのDELETE時にpinsがCASCADE削除されること"""

    def _add_decision_to(self, topic_id: int) -> int:
        res = add_decisions([{"topic_id": topic_id, "decision": "d", "reason": "r"}])
        return res["created"][0]["decision_id"]

    def test_topic_delete_cascades_pins(self, topic, activity):
        topic_id = topic["topic_id"]
        activity_id = activity["activity_id"]
        add_pin("topic", topic_id, "activity", activity_id)
        conn = get_connection()
        try:
            conn.execute("DELETE FROM discussion_topics WHERE id=?", (topic_id,))
            conn.commit()
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM pins WHERE "
                "(source_type='topic' AND source_id=?) OR (target_type='topic' AND target_id=?)",
                (topic_id, topic_id),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert cnt == 0

    def test_activity_delete_cascades_pins(self, topic, activity, material):
        activity_id = activity["activity_id"]
        material_id = material["material_id"]
        add_pin("activity", activity_id, "material", material_id)
        conn = get_connection()
        try:
            conn.execute("DELETE FROM activities WHERE id=?", (activity_id,))
            conn.commit()
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM pins WHERE "
                "(source_type='activity' AND source_id=?) OR (target_type='activity' AND target_id=?)",
                (activity_id, activity_id),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert cnt == 0

    def test_material_delete_cascades_pins(self, topic, activity, material):
        activity_id = activity["activity_id"]
        material_id = material["material_id"]
        add_pin("activity", activity_id, "material", material_id)
        conn = get_connection()
        try:
            conn.execute("DELETE FROM materials WHERE id=?", (material_id,))
            conn.commit()
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM pins WHERE "
                "(source_type='material' AND source_id=?) OR (target_type='material' AND target_id=?)",
                (material_id, material_id),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert cnt == 0

    def test_decision_delete_cascades_pins(self, topic, activity):
        topic_id = topic["topic_id"]
        activity_id = activity["activity_id"]
        decision_id = self._add_decision_to(topic_id)
        add_pin("activity", activity_id, "decision", decision_id)
        conn = get_connection()
        try:
            conn.execute("DELETE FROM decisions WHERE id=?", (decision_id,))
            conn.commit()
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM pins WHERE "
                "(source_type='decision' AND source_id=?) OR (target_type='decision' AND target_id=?)",
                (decision_id, decision_id),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert cnt == 0

    def test_log_delete_cascades_pins(self, topic, activity):
        topic_id = topic["topic_id"]
        activity_id = activity["activity_id"]
        log_res = add_logs([{"topic_id": topic_id, "content": "log", "title": "log title"}])
        log_id = log_res["created"][0]["log_id"]
        add_pin("activity", activity_id, "log", log_id)
        conn = get_connection()
        try:
            conn.execute("DELETE FROM discussion_logs WHERE id=?", (log_id,))
            conn.commit()
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM pins WHERE "
                "(source_type='log' AND source_id=?) OR (target_type='log' AND target_id=?)",
                (log_id, log_id),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert cnt == 0

    def test_tag_delete_cascades_pins(self, topic, activity, temp_db):
        activity_id = activity["activity_id"]
        conn = get_connection()
        try:
            tag_ids = ensure_tag_ids(conn, [("domain", "calm")])
            conn.commit()
            tag_id = tag_ids[0]
        finally:
            conn.close()
        add_pin("tag", tag_id, "activity", activity_id)
        conn = get_connection()
        try:
            conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))
            conn.commit()
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM pins WHERE "
                "(source_type='tag' AND source_id=?) OR (target_type='tag' AND target_id=?)",
                (tag_id, tag_id),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert cnt == 0
