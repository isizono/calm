"""activity 表示用の readable ID 整形 helper

session_start_hook の activity 一覧表示で、整数 ID を `{title} (#NNN)` 形式の
文字列に整形するために使う。tool レスポンス整形 (id 削除 + id_raw 退避、
src/services/readable_id.py) とは別経路であり、新規 caller を増やさないこと。
"""
from typing import Literal, get_args

ENTITY_TYPE = Literal["topic", "decision", "activity", "log", "material", "signal"]
FLAVOR = Literal["raw", "internal", "readable"]

_VALID_ENTITY_TYPES: frozenset[str] = frozenset(get_args(ENTITY_TYPE))


def format_readable_id(
    entity_type: ENTITY_TYPE,
    id_int: int,
    title: str | None,
    flavor: FLAVOR = "readable",
) -> str:
    """エンティティ ID を `{title} (#NNN)` 形式の文字列で返す。

    Args:
        entity_type: エンティティ種別 ('topic'/'decision'/'activity'/'log'/'material'/'signal')
        id_int: 元の整数 ID
        title: エンティティのタイトル (None や空文字列の場合は ID のみ)
        flavor: 表示形式 ('readable' のみ実装済み、'raw'/'internal' は将来実装)

    Returns:
        flavor='readable': `{title} (#{id_int})` 形式の文字列。
            title が None または空文字列の場合は `(#{id_int})` のみ。

    Raises:
        ValueError: entity_type が不正な場合
        NotImplementedError: flavor='raw' / 'internal' は将来実装予定
    """
    if entity_type not in _VALID_ENTITY_TYPES:
        raise ValueError(
            f"Invalid entity_type: {entity_type!r}. "
            f"Must be one of {sorted(_VALID_ENTITY_TYPES)}"
        )

    if flavor == "readable":
        if title:
            return f"{title} (#{id_int})"
        return f"(#{id_int})"

    if flavor in ("raw", "internal"):
        raise NotImplementedError(
            f"flavor={flavor!r} は将来実装予定"
        )

    raise ValueError(
        f"Invalid flavor: {flavor!r}. Must be one of 'raw', 'internal', 'readable'"
    )
