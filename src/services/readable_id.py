"""エンティティ ID 整形 helper

cc-memory の get 系 tool 返却で、整数 `id` を `id_raw` に退避し、`id` キーを削除する。
タイトル + (#NNN) 形式の文字列展開は廃止 (D#2966)。AI には構造化フィールド (`title` +
`id_raw`) で渡し、内部 ID リテラルが返却ペイロードに乗らないようにする。

`format_readable_id` は session_start_hook の activity 表示で参照されているため残置。
"""
from typing import Literal, get_args

ENTITY_TYPE = Literal["topic", "decision", "activity", "log", "material"]
FLAVOR = Literal["raw", "internal", "readable"]

_VALID_ENTITY_TYPES: frozenset[str] = frozenset(get_args(ENTITY_TYPE))


def format_readable_id(
    entity_type: ENTITY_TYPE,
    id_int: int,
    title: str | None,
    flavor: FLAVOR = "readable",
) -> str:
    """エンティティ ID を `{title} (#NNN)` 形式の文字列で返す。

    現状は session_start_hook の activity 表示のみが利用する。tool レスポンス整形は
    `apply_readable_id_inplace` で別経路 (id 削除 + id_raw 退避) に切り替わったため、
    新規 caller を増やさないこと。

    Args:
        entity_type: エンティティ種別 ('topic'/'decision'/'activity'/'log'/'material')
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


def apply_readable_id_inplace(
    result_dict: dict,
    entity_type: ENTITY_TYPE,
    id_key: str = "id",
) -> None:
    """`result_dict[id_key]` を `{id_key}_raw` に退避し、元キーを削除する (in-place)。

    AI 側に渡るペイロードから内部 ID リテラル (`(#NNN)` 文字列) を消すために、
    整数 `id` を `id_raw` 名で残しつつ `id` キー自体を取り除く。`title` 等の他フィールド
    は触らない。

    冪等性:
    - `id_key` が `result_dict` に無ければ no-op
    - `{id_key}_raw` が既に存在すれば no-op (既に処理済み)
    - `result_dict[id_key]` が int でなければ no-op (既に文字列に整形済み等)

    Args:
        result_dict: 対象の dict (in-place 更新される)
        entity_type: エンティティ種別 (検証のみに使用、戻り値・更新内容には影響しない)
        id_key: ID を保持するキー名 (デフォルト "id")
    """
    if entity_type not in _VALID_ENTITY_TYPES:
        raise ValueError(
            f"Invalid entity_type: {entity_type!r}. "
            f"Must be one of {sorted(_VALID_ENTITY_TYPES)}"
        )

    if id_key not in result_dict:
        return

    raw_key = f"{id_key}_raw"
    if raw_key in result_dict:
        return

    id_value = result_dict[id_key]
    if not isinstance(id_value, int):
        return

    result_dict[raw_key] = id_value
    del result_dict[id_key]
