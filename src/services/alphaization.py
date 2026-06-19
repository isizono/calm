"""エンティティ ID α化 helper (T#465 Phase 1)

cc-memory の MCP tool 返却で `id` フィールドが整数として返り、AI が文脈推測なしで
「何を指しているか」読めるよう、タイトル + (#NNN) 形式に展開する。

Phase 1: flavor="readable" のみ実装。
Phase 3 (D#2724): flavor="raw" / flavor="internal" を追加し、citation parser と接続する。
"""
from typing import Literal

ENTITY_TYPE = Literal["topic", "decision", "activity", "log", "material"]
FLAVOR = Literal["raw", "internal", "readable"]

_VALID_ENTITY_TYPES: frozenset[str] = frozenset(
    {"topic", "decision", "activity", "log", "material"}
)


def alphaize_entity_id(
    entity_type: ENTITY_TYPE,
    id_int: int,
    title: str | None,
    flavor: FLAVOR = "readable",
) -> str | int:
    """エンティティ ID を α化形式で返す。

    Phase 1 では flavor='readable' のみ実装。Phase 3 で raw / internal を追加予定。

    Args:
        entity_type: エンティティ種別 ('topic'/'decision'/'activity'/'log'/'material')
        id_int: 元の整数 ID
        title: エンティティのタイトル（None や空文字列の場合は ID のみ）
        flavor: 表示形式（'readable' のみ実装済み、'raw'/'internal' は Phase 3 で実装）

    Returns:
        flavor='readable': `{title} (#{id_int})` 形式の文字列。
            title が None または空文字列の場合は `(#{id_int})` のみ。

    Raises:
        ValueError: entity_type が不正な場合
        NotImplementedError: flavor='raw' / 'internal' は Phase 3 で実装予定
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
            f"flavor={flavor!r} は Phase 3 で実装予定 (D#2697 / D#2698)"
        )

    raise ValueError(
        f"Invalid flavor: {flavor!r}. Must be one of 'raw', 'internal', 'readable'"
    )


def alphaize_result_dict_inplace(
    result_dict: dict,
    entity_type: ENTITY_TYPE,
    flavor: FLAVOR = "readable",
    id_key: str = "id",
    title_key: str = "title",
) -> None:
    """`result_dict[id_key]` を α化形式で置換する（in-place）。

    元の整数 ID は `{id_key}_raw` に退避し、後段 (Phase 3 parser / migration tool)
    からアクセス可能にする。Phase 1 単独では使われないが、Phase 3 接続のため残す。

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

    # 冪等性: すでに α化済み（_raw に元 ID が退避されている）なら何もしない
    raw_key = f"{id_key}_raw"
    if raw_key in result_dict:
        return

    id_value = result_dict[id_key]
    # 整数以外（既に α化文字列など）が入っている場合は触らない
    if not isinstance(id_value, int):
        return

    title = result_dict.get(title_key)
    result_dict[raw_key] = id_value
    result_dict[id_key] = alphaize_entity_id(entity_type, id_value, title, flavor)
