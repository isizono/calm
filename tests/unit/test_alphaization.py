"""alphaization helper の単体テスト (T#465 Phase 1)

`alphaize_entity_id` / `alphaize_result_dict_inplace` の挙動を、
5 entity type × normal/missing title/empty title の組み合わせで検証する。
"""
import pytest

from src.services.alphaization import (
    alphaize_entity_id,
    alphaize_result_dict_inplace,
)


# --- alphaize_entity_id: 5 entity type × normal title ---


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
def test_alphaize_entity_id_readable_normal(entity_type, id_int, title, expected):
    """5 entity type で title 付きの readable 形式が組み立てられる"""
    assert alphaize_entity_id(entity_type, id_int, title) == expected


def test_alphaize_entity_id_missing_title():
    """title=None の場合は (#NNN) のみ"""
    assert alphaize_entity_id("topic", 100, None) == "(#100)"


def test_alphaize_entity_id_empty_title():
    """title="" の場合は (#NNN) のみ"""
    assert alphaize_entity_id("decision", 200, "") == "(#200)"


def test_alphaize_entity_id_explicit_readable_flavor():
    """flavor='readable' を明示指定しても同じ結果"""
    assert alphaize_entity_id("activity", 5, "title", flavor="readable") == "title (#5)"


# --- error cases ---


def test_alphaize_entity_id_invalid_entity_type():
    """不正な entity_type は ValueError"""
    with pytest.raises(ValueError, match="Invalid entity_type"):
        alphaize_entity_id("unknown", 1, "x")  # type: ignore[arg-type]


def test_alphaize_entity_id_raw_flavor_not_implemented():
    """flavor='raw' は Phase 3 で実装予定"""
    with pytest.raises(NotImplementedError, match="Phase 3"):
        alphaize_entity_id("topic", 1, "x", flavor="raw")


def test_alphaize_entity_id_internal_flavor_not_implemented():
    """flavor='internal' は Phase 3 で実装予定"""
    with pytest.raises(NotImplementedError, match="Phase 3"):
        alphaize_entity_id("topic", 1, "x", flavor="internal")


# --- alphaize_result_dict_inplace ---


def test_alphaize_result_dict_inplace_normal():
    """通常 dict は id を α化し、id_raw に元 ID を退避する"""
    d = {"id": 123, "title": "T", "other": "x"}
    alphaize_result_dict_inplace(d, "topic")
    assert d == {"id": "T (#123)", "id_raw": 123, "title": "T", "other": "x"}


def test_alphaize_result_dict_inplace_missing_title():
    """title キーが無い場合は (#NNN) のみ"""
    d = {"id": 50}
    alphaize_result_dict_inplace(d, "log")
    assert d == {"id": "(#50)", "id_raw": 50}


def test_alphaize_result_dict_inplace_empty_title():
    """title が空文字列の場合は (#NNN) のみ"""
    d = {"id": 9, "title": ""}
    alphaize_result_dict_inplace(d, "material")
    assert d == {"id": "(#9)", "id_raw": 9, "title": ""}


def test_alphaize_result_dict_inplace_none_title():
    """title が None の場合は (#NNN) のみ"""
    d = {"id": 11, "title": None}
    alphaize_result_dict_inplace(d, "decision")
    assert d == {"id": "(#11)", "id_raw": 11, "title": None}


def test_alphaize_result_dict_inplace_missing_id_key():
    """id_key が無い場合は何もしない"""
    d = {"title": "x"}
    alphaize_result_dict_inplace(d, "topic")
    assert d == {"title": "x"}


def test_alphaize_result_dict_inplace_custom_keys():
    """id_key / title_key を変更できる"""
    d = {"decision_id": 7, "decision_title": "D"}
    alphaize_result_dict_inplace(
        d, "decision", id_key="decision_id", title_key="decision_title"
    )
    assert d == {
        "decision_id": "D (#7)",
        "decision_id_raw": 7,
        "decision_title": "D",
    }


def test_alphaize_result_dict_inplace_idempotent():
    """すでに α化済み（id_raw が存在）の dict には何もしない"""
    d = {"id": "T (#1)", "id_raw": 1, "title": "T"}
    alphaize_result_dict_inplace(d, "topic")
    assert d == {"id": "T (#1)", "id_raw": 1, "title": "T"}


def test_alphaize_result_dict_inplace_non_int_id():
    """id が int でない（既に α化文字列など）場合は何もしない"""
    d = {"id": "already (#1)", "title": "x"}
    alphaize_result_dict_inplace(d, "topic")
    assert d == {"id": "already (#1)", "title": "x"}
