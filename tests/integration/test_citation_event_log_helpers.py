"""citation_event_log helper (record_citation_event / apply_raw_to_cite_conversion)
の統合テスト

migration 0046 適用後に、citations_service の 2 helper が
- record_citation_event: 1 行 INSERT して row id を返す / source / target_entity_type /
  verification_result の値ドメイン違反を ValueError で弾く / extra dict を JSON で格納
- apply_raw_to_cite_conversion: OWNER_TEXT_FIELDS field map を引いて raw `X#NNN` を
  `{{cite:X#NNN}}` に変換 / 既存 cite はスキップで冪等 / dangling target は
  `[deleted X#NNN]` に確定書き換え / 変換のあった field 単位で event を 1 件記録
を満たすことを確認する。
"""
import json
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.citations_service import (
    VALID_EVENT_SOURCES,
    VALID_VERIFICATION_RESULTS,
    apply_and_writeback_conversions,
    apply_raw_to_cite_conversion,
    record_citation_event,
)


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _seed_material(title: str = "tgt", content: str = "body") -> int:
    """seed 用に materials へ 1 行 INSERT して id を返す。"""
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


def _fetch_event(event_id: int) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, source, tool_name, target_entity_type, target_entity_id, "
            "target_field, before_text, after_text, verified_at, "
            "verification_result, extra_json "
            "FROM citation_event_log WHERE id = ?",
            (event_id,),
        ).fetchone()
        return dict(row) if row else None
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


def _all_event_rows() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, source, target_entity_type, target_entity_id, target_field, "
            "before_text, after_text, verification_result FROM citation_event_log "
            "ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ============================================================
# record_citation_event 単体
# ============================================================
class TestRecordCitationEvent:
    def test_returns_inserted_row_id(self, temp_db):
        """row id が返り、その id で fetch できる"""
        conn = get_connection()
        try:
            with conn:
                event_id = record_citation_event(
                    conn,
                    source="write_auto_convert",
                    tool_name="add_material",
                    target_entity_type="material",
                    target_entity_id=42,
                    target_field="content",
                    before_text="raw M#1",
                    after_text="{{cite:M#1}}",
                    verification_result="exists",
                )
        finally:
            conn.close()
        assert isinstance(event_id, int) and event_id > 0
        row = _fetch_event(event_id)
        assert row["source"] == "write_auto_convert"
        assert row["tool_name"] == "add_material"
        assert row["target_entity_type"] == "material"
        assert row["target_entity_id"] == 42
        assert row["target_field"] == "content"
        assert row["before_text"] == "raw M#1"
        assert row["after_text"] == "{{cite:M#1}}"
        assert row["verification_result"] == "exists"

    def test_extra_is_stored_as_json(self, temp_db):
        """extra (dict) は JSON 文字列として extra_json に格納される"""
        conn = get_connection()
        try:
            with conn:
                event_id = record_citation_event(
                    conn,
                    source="bulk_migration",
                    tool_name=None,
                    target_entity_type=None,
                    target_entity_id=None,
                    target_field=None,
                    before_text="b",
                    after_text="a",
                    extra={"sanitized_count": 3, "skipped_in_codeblock": 1},
                )
        finally:
            conn.close()
        row = _fetch_event(event_id)
        assert row["extra_json"] is not None
        decoded = json.loads(row["extra_json"])
        assert decoded == {"sanitized_count": 3, "skipped_in_codeblock": 1}

    def test_extra_none_yields_null_extra_json(self, temp_db):
        """extra=None なら extra_json は NULL"""
        conn = get_connection()
        try:
            with conn:
                event_id = record_citation_event(
                    conn,
                    source="external_doc_sanitize",
                    tool_name=None,
                    target_entity_type=None,
                    target_entity_id=None,
                    target_field=None,
                    before_text="b",
                    after_text="a",
                )
        finally:
            conn.close()
        row = _fetch_event(event_id)
        assert row["extra_json"] is None

    @pytest.mark.parametrize("source", list(VALID_EVENT_SOURCES))
    def test_all_valid_sources_accepted(self, temp_db, source):
        """source の許容 5 値はすべて受け入れる"""
        conn = get_connection()
        try:
            with conn:
                event_id = record_citation_event(
                    conn,
                    source=source,
                    tool_name=None,
                    target_entity_type=None,
                    target_entity_id=None,
                    target_field=None,
                    before_text="b",
                    after_text="a",
                )
        finally:
            conn.close()
        assert event_id > 0

    def test_invalid_source_raises_value_error(self, temp_db):
        """source が許容値以外なら ValueError (DB に到達する前に弾く)"""
        conn = get_connection()
        try:
            with pytest.raises(ValueError, match="Invalid source"):
                record_citation_event(
                    conn,
                    source="unknown",
                    tool_name=None,
                    target_entity_type=None,
                    target_entity_id=None,
                    target_field=None,
                    before_text="b",
                    after_text="a",
                )
        finally:
            conn.close()

    def test_invalid_target_entity_type_raises_value_error(self, temp_db):
        """target_entity_type が許容値以外なら ValueError"""
        conn = get_connection()
        try:
            with pytest.raises(ValueError, match="Invalid target_entity_type"):
                record_citation_event(
                    conn,
                    source="write_auto_convert",
                    tool_name=None,
                    target_entity_type="habit",
                    target_entity_id=1,
                    target_field=None,
                    before_text="b",
                    after_text="a",
                )
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "verification_result", list(VALID_VERIFICATION_RESULTS)
    )
    def test_all_valid_verification_results_accepted(
        self, temp_db, verification_result
    ):
        """verification_result の許容 3 値はすべて受け入れる"""
        conn = get_connection()
        try:
            with conn:
                event_id = record_citation_event(
                    conn,
                    source="write_auto_convert",
                    tool_name=None,
                    target_entity_type=None,
                    target_entity_id=None,
                    target_field=None,
                    before_text="b",
                    after_text="a",
                    verification_result=verification_result,
                )
        finally:
            conn.close()
        assert event_id > 0

    def test_invalid_verification_result_raises_value_error(self, temp_db):
        """verification_result が許容値以外なら ValueError"""
        conn = get_connection()
        try:
            with pytest.raises(ValueError, match="Invalid verification_result"):
                record_citation_event(
                    conn,
                    source="write_auto_convert",
                    tool_name=None,
                    target_entity_type=None,
                    target_entity_id=None,
                    target_field=None,
                    before_text="b",
                    after_text="a",
                    verification_result="undecided",
                )
        finally:
            conn.close()


# ============================================================
# apply_raw_to_cite_conversion: 冪等性
# ============================================================
class TestApplyRawToCiteIdempotency:
    def test_already_cite_format_skipped(self, temp_db):
        """既に `{{cite:X#NNN}}` 形式の本文は無加工 (冪等)、event 記録なし"""
        target_id = _seed_material()
        original = f"existing {{{{cite:M#{target_id}}}}} only"
        conn = get_connection()
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="material",
                    entity_id=999,
                    fields_payload={"title": "t", "content": original},
                    tool_name="add_material",
                )
        finally:
            conn.close()
        # 入力と出力が完全一致
        assert res["fields"]["content"] == original
        # 変化なしなので event 記録なし
        assert res["event_ids"] == []
        assert _all_event_rows() == []

    def test_applying_twice_is_idempotent(self, temp_db):
        """1 回目の変換結果に対する 2 回目は無加工 (idempotent)"""
        target_id = _seed_material()
        raw = f"see M#{target_id} here"
        conn = get_connection()
        try:
            with conn:
                first = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="material",
                    entity_id=999,
                    fields_payload={"title": "t", "content": raw},
                    tool_name="t",
                )
            with conn:
                second = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="material",
                    entity_id=999,
                    fields_payload={"title": "t", "content": first["fields"]["content"]},
                    tool_name="t",
                )
        finally:
            conn.close()
        assert first["fields"]["content"] == f"see {{{{cite:M#{target_id}}}}} here"
        # 2回目は変化なし
        assert second["fields"]["content"] == first["fields"]["content"]
        assert second["event_ids"] == []


# ============================================================
# apply_raw_to_cite_conversion: dangling 検出パス
# ============================================================
class TestApplyRawToCiteDangling:
    def test_dangling_target_rewritten_to_deleted_marker(self, temp_db):
        """存在しない target は `[deleted X#NNN]` に確定書き換え"""
        # 何も seed しない (M#1 は存在しない)
        conn = get_connection()
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="material",
                    entity_id=999,
                    fields_payload={"title": "t", "content": "lost M#1 forever"},
                    tool_name="add_material",
                )
        finally:
            conn.close()
        assert res["fields"]["content"] == "lost [deleted M#1] forever"
        # event 1 件、verification_result=dangling
        assert len(res["event_ids"]) == 1
        event = _fetch_event(res["event_ids"][0])
        assert event["verification_result"] == "dangling"
        assert event["target_field"] == "content"
        assert event["before_text"] == "lost M#1 forever"
        assert event["after_text"] == "lost [deleted M#1] forever"
        decoded = json.loads(event["extra_json"])
        assert decoded["dangling_count"] == 1
        assert decoded["dangling_targets"] == [{"type": "material", "id": 1}]

    def test_dangling_inside_codeblock_is_preserved(self, temp_db):
        """同一 dangling target がコードブロックの内外に混在しても、コードブロック内の
        リテラルは raw のまま温存される (= スキップ区間内は書き換え対象外)。"""
        # M#1 は seed しない (= dangling)
        conn = get_connection()
        original = "```\nM#1\n```\n\nSee M#1 here"
        expected = "```\nM#1\n```\n\nSee [deleted M#1] here"
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="material",
                    entity_id=999,
                    fields_payload={"title": "t", "content": original},
                    tool_name="add_material",
                )
        finally:
            conn.close()
        assert res["fields"]["content"] == expected

    def test_dangling_inside_inline_backticks_is_preserved(self, temp_db):
        """インラインバッククォート内の同一 dangling リテラルも温存される。"""
        conn = get_connection()
        original = "in code `M#1` and outside M#1"
        expected = "in code `M#1` and outside [deleted M#1]"
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="material",
                    entity_id=999,
                    fields_payload={"title": "t", "content": original},
                    tool_name="add_material",
                )
        finally:
            conn.close()
        assert res["fields"]["content"] == expected

    def test_dangling_occurrence_count_vs_unique_count(self, temp_db):
        """同一 dangling target が複数回出ると、unique 数と総置換数が乖離する。
        stats は両方を提供する。"""
        conn = get_connection()
        # M#1 (dangling) が変換対象区間に 3 回登場
        original = "first M#1, second M#1, third M#1"
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="material",
                    entity_id=999,
                    fields_payload={"title": "t", "content": original},
                    tool_name="add_material",
                )
        finally:
            conn.close()
        # 全 3 箇所が [deleted M#1] に書き換わる
        assert res["fields"]["content"] == (
            "first [deleted M#1], second [deleted M#1], third [deleted M#1]"
        )
        field_stats = res["stats"]["content"]
        # unique target 数 = 1, 総置換回数 = 3
        assert field_stats["dangling_count"] == 1
        assert field_stats["dangling_occurrence_count"] == 3

    def test_mixed_existing_and_dangling(self, temp_db):
        """同一 field 内に 存在 target / dangling target が混在しても両方処理"""
        target_id = _seed_material()
        original = f"have M#{target_id} and lost M#9999"
        conn = get_connection()
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="material",
                    entity_id=999,
                    fields_payload={"title": "t", "content": original},
                    tool_name="add_material",
                )
        finally:
            conn.close()
        assert res["fields"]["content"] == (
            f"have {{{{cite:M#{target_id}}}}} and lost [deleted M#9999]"
        )
        assert len(res["event_ids"]) == 1
        event = _fetch_event(res["event_ids"][0])
        # 1 件でも dangling があれば verification_result=dangling
        assert event["verification_result"] == "dangling"
        decoded = json.loads(event["extra_json"])
        assert decoded["sanitized_count"] == 1
        assert decoded["dangling_count"] == 1


# ============================================================
# apply_raw_to_cite_conversion: 全 field マッピング
# ============================================================
class TestApplyRawToCiteFieldMapping:
    """OWNER_TEXT_FIELDS 全 entity_type で対象 field が変換され、対象外 key は素通し"""

    def test_material_title_and_content_converted(self, temp_db):
        """material: title / content 両方が変換対象"""
        target_id = _seed_material()
        conn = get_connection()
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="material",
                    entity_id=999,
                    fields_payload={
                        "title": f"M#{target_id} mention",
                        "content": f"see M#{target_id}",
                        "extra_key": "raw M#1 keeps untouched",
                    },
                    tool_name="add_material",
                )
        finally:
            conn.close()
        assert res["fields"]["title"] == f"{{{{cite:M#{target_id}}}}} mention"
        assert res["fields"]["content"] == f"see {{{{cite:M#{target_id}}}}}"
        # 対象外 key は素通し
        assert res["fields"]["extra_key"] == "raw M#1 keeps untouched"
        assert len(res["event_ids"]) == 2  # title / content 各 1 件

    def test_decision_decision_and_reason_converted(self, temp_db):
        """decision: decision / reason 両方が変換対象"""
        target_id = _seed_material()
        conn = get_connection()
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="decision",
                    entity_id=1,
                    fields_payload={
                        "decision": f"adopt M#{target_id}",
                        "reason": f"because of M#{target_id}",
                    },
                    tool_name="add_decisions",
                )
        finally:
            conn.close()
        assert res["fields"]["decision"] == f"adopt {{{{cite:M#{target_id}}}}}"
        assert res["fields"]["reason"] == f"because of {{{{cite:M#{target_id}}}}}"
        assert len(res["event_ids"]) == 2

    def test_log_content_converted(self, temp_db):
        """log: content のみが変換対象"""
        target_id = _seed_material()
        conn = get_connection()
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="log",
                    entity_id=1,
                    fields_payload={"content": f"talked about M#{target_id}"},
                    tool_name="add_logs",
                )
        finally:
            conn.close()
        assert res["fields"]["content"] == (
            f"talked about {{{{cite:M#{target_id}}}}}"
        )
        assert len(res["event_ids"]) == 1

    def test_activity_title_and_description_converted(self, temp_db):
        """activity: title / description 両方が変換対象"""
        target_id = _seed_material()
        conn = get_connection()
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="activity",
                    entity_id=1,
                    fields_payload={
                        "title": f"M#{target_id} work",
                        "description": f"plan around M#{target_id}",
                    },
                    tool_name="add_activity",
                )
        finally:
            conn.close()
        assert res["fields"]["title"] == f"{{{{cite:M#{target_id}}}}} work"
        assert res["fields"]["description"] == f"plan around {{{{cite:M#{target_id}}}}}"
        assert len(res["event_ids"]) == 2

    def test_topic_title_and_description_converted(self, temp_db):
        """topic: title / description 両方が変換対象"""
        target_id = _seed_material()
        conn = get_connection()
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="topic",
                    entity_id=1,
                    fields_payload={
                        "title": f"M#{target_id} discussion",
                        "description": f"explore M#{target_id}",
                    },
                    tool_name="add_topic",
                )
        finally:
            conn.close()
        assert res["fields"]["title"] == f"{{{{cite:M#{target_id}}}}} discussion"
        assert res["fields"]["description"] == f"explore {{{{cite:M#{target_id}}}}}"
        assert len(res["event_ids"]) == 2

    def test_invalid_entity_type_raises_value_error(self, temp_db):
        """OWNER_TEXT_FIELDS にない entity_type は ValueError"""
        conn = get_connection()
        try:
            with pytest.raises(ValueError, match="Invalid entity_type"):
                apply_raw_to_cite_conversion(
                    conn,
                    entity_type="habit",
                    entity_id=1,
                    fields_payload={"title": "anything"},
                    tool_name="add_habit",
                )
        finally:
            conn.close()

    def test_empty_field_value_skipped(self, temp_db):
        """field 値が空文字 or None なら処理対象外 (event 記録なし)"""
        conn = get_connection()
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="material",
                    entity_id=1,
                    fields_payload={"title": "", "content": None},
                    tool_name="add_material",
                )
        finally:
            conn.close()
        assert res["event_ids"] == []
        assert _all_event_rows() == []

    def test_tool_name_is_recorded_in_event(self, temp_db):
        """event の tool_name 列に呼び出し時の tool_name が格納される"""
        target_id = _seed_material()
        conn = get_connection()
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="log",
                    entity_id=1,
                    fields_payload={"content": f"say M#{target_id}"},
                    tool_name="some_write_tool",
                )
        finally:
            conn.close()
        event = _fetch_event(res["event_ids"][0])
        assert event["tool_name"] == "some_write_tool"

    def test_verified_at_is_stamped_when_event_recorded(self, temp_db):
        """event 記録時に verified_at に UTC タイムスタンプが入る"""
        target_id = _seed_material()
        conn = get_connection()
        try:
            with conn:
                res = apply_raw_to_cite_conversion(
                    conn,
                    entity_type="log",
                    entity_id=1,
                    fields_payload={"content": f"say M#{target_id}"},
                    tool_name="t",
                )
        finally:
            conn.close()
        event = _fetch_event(res["event_ids"][0])
        assert event["verified_at"] is not None
        assert len(event["verified_at"]) >= 19


# ============================================================
# apply_and_writeback_conversions
# ============================================================
class TestApplyAndWritebackConversions:
    def test_none_field_excluded_and_passed_through(self, temp_db):
        """None値のfieldは変換対象から除外され、DBにも書き戻されず、
        戻り値の merged dict では None のまま返る"""
        target_id = _seed_material()
        material_id = _seed_material(title="orig title", content=f"see M#{target_id}")
        conn = get_connection()
        try:
            with conn:
                merged = apply_and_writeback_conversions(
                    conn,
                    entity_type="material",
                    entity_id=material_id,
                    fields_payload={"title": None, "content": f"see M#{target_id}"},
                    tool_name="update_material",
                    table="materials",
                )
        finally:
            conn.close()
        assert merged["title"] is None
        assert merged["content"] == f"see {{{{cite:M#{target_id}}}}}"
        row = _fetch_material(material_id)
        # title は fields_payload で None だったので変換対象外、DB の既存値も不変
        assert row["title"] == "orig title"
        assert row["content"] == f"see {{{{cite:M#{target_id}}}}}"

    def test_changed_field_is_written_back_to_db(self, temp_db):
        """本文が変換で書き換わったfieldはDBへUPDATEで書き戻される"""
        target_id = _seed_material()
        material_id = _seed_material(title="t", content=f"see M#{target_id}")
        conn = get_connection()
        try:
            with conn:
                apply_and_writeback_conversions(
                    conn,
                    entity_type="material",
                    entity_id=material_id,
                    fields_payload={"title": "t", "content": f"see M#{target_id}"},
                    tool_name="add_material",
                    table="materials",
                )
        finally:
            conn.close()
        row = _fetch_material(material_id)
        assert row["content"] == f"see {{{{cite:M#{target_id}}}}}"

    def test_unchanged_field_db_value_stays_identical(self, temp_db):
        """既にcite形式で変換不要なfieldは、書き戻し後もDB値が元のまま"""
        target_id = _seed_material()
        original = f"existing {{{{cite:M#{target_id}}}}} only"
        material_id = _seed_material(title="t", content=original)
        conn = get_connection()
        try:
            with conn:
                merged = apply_and_writeback_conversions(
                    conn,
                    entity_type="material",
                    entity_id=material_id,
                    fields_payload={"title": "t", "content": original},
                    tool_name="add_material",
                    table="materials",
                )
        finally:
            conn.close()
        assert merged["content"] == original
        row = _fetch_material(material_id)
        assert row["content"] == original

    def test_merged_return_combines_converted_and_untouched_keys(self, temp_db):
        """戻り値の merged dict は、変換対象keyは変換後の値、対象外keyは元の値をそのまま持つ"""
        target_id = _seed_material()
        material_id = _seed_material(title="t", content="body")
        conn = get_connection()
        try:
            with conn:
                merged = apply_and_writeback_conversions(
                    conn,
                    entity_type="material",
                    entity_id=material_id,
                    fields_payload={
                        "title": "t",
                        "content": f"see M#{target_id}",
                        "extra_key": "untouched",
                    },
                    tool_name="add_material",
                    table="materials",
                )
        finally:
            conn.close()
        assert merged["title"] == "t"
        assert merged["content"] == f"see {{{{cite:M#{target_id}}}}}"
        assert merged["extra_key"] == "untouched"
