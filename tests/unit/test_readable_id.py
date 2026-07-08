"""readable_id helper (strip_entity_id_inplace) の単体テスト"""
import pytest

from src.services.readable_id import strip_entity_id_inplace


def test_strip_entity_id_inplace_normal():
    """通常 dict は id を削除し、id_raw に元 ID (int) を退避する。title は触らない"""
    d = {"id": 123, "title": "T", "other": "x"}
    strip_entity_id_inplace(d)
    assert d == {"id_raw": 123, "title": "T", "other": "x"}


def test_strip_entity_id_inplace_does_not_touch_title():
    """title フィールドは値の有無に関わらず一切変更されない"""
    d = {"id": 50}
    strip_entity_id_inplace(d)
    assert d == {"id_raw": 50}

    d2 = {"id": 9, "title": ""}
    strip_entity_id_inplace(d2)
    assert d2 == {"id_raw": 9, "title": ""}

    d3 = {"id": 11, "title": None}
    strip_entity_id_inplace(d3)
    assert d3 == {"id_raw": 11, "title": None}


def test_strip_entity_id_inplace_missing_id_key():
    """id_key が無い場合は何もしない"""
    d = {"title": "x"}
    strip_entity_id_inplace(d)
    assert d == {"title": "x"}


def test_strip_entity_id_inplace_custom_id_key():
    """id_key を変更できる"""
    d = {"decision_id": 7, "decision_title": "D"}
    strip_entity_id_inplace(d, id_key="decision_id")
    assert d == {
        "decision_id_raw": 7,
        "decision_title": "D",
    }


def test_strip_entity_id_inplace_idempotent():
    """すでに整形済み (id_raw が存在) の dict には何もしない"""
    d = {"id_raw": 1, "title": "T"}
    strip_entity_id_inplace(d)
    assert d == {"id_raw": 1, "title": "T"}


def test_strip_entity_id_inplace_non_int_id():
    """id が int でない場合 (report_signal の自由形式 context 経由等) は何もしない"""
    d = {"id": "already (#1)", "title": "x"}
    strip_entity_id_inplace(d)
    assert d == {"id": "already (#1)", "title": "x"}
