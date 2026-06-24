"""readable_id helper の単体テスト

`format_readable_id` / `apply_readable_id_inplace` の挙動を、
5 entity type × normal/missing title/empty title の組み合わせで検証する。
"""
import pytest

from src.services.readable_id import (
    apply_readable_id_inplace,
    format_readable_id,
)


# --- format_readable_id: 5 entity type × normal title ---


@pytest.mark.parametrize(
    "entity_type,id_int,title,expected",
    [
        ("topic", 123, "my topic", "my topic (#123)"),
        ("decision", 456, "ある決定", "ある決定 (#456)"),
        ("activity", 789, "[作業] something", "[作業] something (#789)"),
        ("log", 42, "log title", "log title (#42)"),
        ("material", 7, "material title", "material title (#7)"),
    ],
)
def test_format_readable_id_normal(entity_type, id_int, title, expected):
    """5 entity type で title 付きの readable 形式が組み立てられる"""
    assert format_readable_id(entity_type, id_int, title) == expected


def test_format_readable_id_missing_title():
    """title=None の場合は (#NNN) のみ"""
    assert format_readable_id("topic", 100, None) == "(#100)"


def test_format_readable_id_empty_title():
    """title="" の場合は (#NNN) のみ"""
    assert format_readable_id("decision", 200, "") == "(#200)"


def test_format_readable_id_explicit_readable_flavor():
    """flavor='readable' を明示指定しても同じ結果"""
    assert format_readable_id("activity", 5, "title", flavor="readable") == "title (#5)"


# --- error cases ---


def test_format_readable_id_invalid_entity_type():
    """不正な entity_type は ValueError"""
    with pytest.raises(ValueError, match="Invalid entity_type"):
        format_readable_id("unknown", 1, "x")  # type: ignore[arg-type]


def test_format_readable_id_raw_flavor_not_implemented():
    """flavor='raw' は将来実装予定"""
    with pytest.raises(NotImplementedError, match="将来実装予定"):
        format_readable_id("topic", 1, "x", flavor="raw")


def test_format_readable_id_internal_flavor_not_implemented():
    """flavor='internal' は将来実装予定"""
    with pytest.raises(NotImplementedError, match="将来実装予定"):
        format_readable_id("topic", 1, "x", flavor="internal")


# --- apply_readable_id_inplace ---


def test_apply_readable_id_inplace_normal():
    """通常 dict は id を削除し、id_raw に元 ID (int) を退避する。title は触らない"""
    d = {"id": 123, "title": "T", "other": "x"}
    apply_readable_id_inplace(d, "topic")
    assert d == {"id_raw": 123, "title": "T", "other": "x"}


def test_apply_readable_id_inplace_does_not_touch_title():
    """title フィールドは値の有無に関わらず一切変更されない"""
    d = {"id": 50}
    apply_readable_id_inplace(d, "log")
    assert d == {"id_raw": 50}

    d2 = {"id": 9, "title": ""}
    apply_readable_id_inplace(d2, "material")
    assert d2 == {"id_raw": 9, "title": ""}

    d3 = {"id": 11, "title": None}
    apply_readable_id_inplace(d3, "decision")
    assert d3 == {"id_raw": 11, "title": None}


def test_apply_readable_id_inplace_missing_id_key():
    """id_key が無い場合は何もしない"""
    d = {"title": "x"}
    apply_readable_id_inplace(d, "topic")
    assert d == {"title": "x"}


def test_apply_readable_id_inplace_custom_id_key():
    """id_key を変更できる"""
    d = {"decision_id": 7, "decision_title": "D"}
    apply_readable_id_inplace(d, "decision", id_key="decision_id")
    assert d == {
        "decision_id_raw": 7,
        "decision_title": "D",
    }


def test_apply_readable_id_inplace_idempotent():
    """すでに整形済み (id_raw が存在) の dict には何もしない"""
    d = {"id_raw": 1, "title": "T"}
    apply_readable_id_inplace(d, "topic")
    assert d == {"id_raw": 1, "title": "T"}


def test_apply_readable_id_inplace_non_int_id():
    """id が int でない場合 (例: 既に文字列化済み) は何もしない"""
    d = {"id": "already (#1)", "title": "x"}
    apply_readable_id_inplace(d, "topic")
    assert d == {"id": "already (#1)", "title": "x"}


def test_apply_readable_id_inplace_invalid_entity_type():
    """不正な entity_type は ValueError"""
    with pytest.raises(ValueError, match="Invalid entity_type"):
        apply_readable_id_inplace({"id": 1}, "unknown")  # type: ignore[arg-type]
