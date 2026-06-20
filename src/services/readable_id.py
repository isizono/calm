"""エンティティ ID readable 整形 helper

cc-memory の MCP tool 返却で `id` フィールドが整数として返り、AI が文脈推測なしで
「何を指しているか」読めるよう、タイトル + (#NNN) 形式に展開する。

現状は flavor="readable" のみ実装。将来 flavor="raw" / flavor="internal" を追加し、
citation parser と接続する。
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
    """エンティティ ID を readable 形式で返す。

    現状は flavor='readable' のみ実装。'raw' / 'internal' は将来拡張用 stub。

    Args:
        entity_type: エンティティ種別 ('topic'/'decision'/'activity'/'log'/'material')
        id_int: 元の整数 ID
        title: エンティティのタイトル（None や空文字列の場合は ID のみ）
        flavor: 表示形式（'readable' のみ実装済み、'raw'/'internal' は将来実装）

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
    flavor: FLAVOR = "readable",
    id_key: str = "id",
    title_key: str = "title",
) -> None:
    """`result_dict[id_key]` を readable 形式で置換する（in-place）。

    元の整数 ID は `{id_key}_raw` に退避し、将来の parser / migration tool から
    アクセス可能にする。

    `id_key` が `result_dict` に存在しない場合や、すでに `{id_key}_raw` が存在する
    場合は何もしない（冪等性確保）。

    Args:
        result_dict: 対象の dict（in-place 更新される）
        entity_type: エンティティ種別
        flavor: 表示形式（'readable' のみ実装済み）
        id_key: ID を保持するキー名（デフォルト "id"）
        title_key: タイトルを保持するキー名（デフォルト "title"）
    """
    if id_key not in result_dict:
        return

    # 冪等性: すでに整形済み（_raw に元 ID が退避されている）なら何もしない
    raw_key = f"{id_key}_raw"
    if raw_key in result_dict:
        return

    id_value = result_dict[id_key]
    # 整数以外（既に整形済み文字列など）が入っている場合は触らない
    if not isinstance(id_value, int):
        return

    title = result_dict.get(title_key)
    result_dict[raw_key] = id_value
    result_dict[id_key] = format_readable_id(entity_type, id_value, title, flavor)
