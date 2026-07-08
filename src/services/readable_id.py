"""エンティティ ID 整形 helper

cc-memory の get 系 tool 返却で、整数 `id` を `id_raw` に退避し、`id` キーを削除する。
AI には構造化フィールド (`title` + `id_raw`) で渡し、内部 ID リテラルが返却ペイロードに
乗らないようにする。
"""


def strip_entity_id_inplace(
    result_dict: dict,
    id_key: str = "id",
) -> None:
    """`result_dict[id_key]` を `{id_key}_raw` に退避し、元キーを削除する (in-place)。

    整数 `id` を `id_raw` 名で残しつつ `id` キー自体を取り除く。`title` 等の他フィールド
    は触らない。

    冪等性:
    - `id_key` が `result_dict` に無ければ no-op
    - `{id_key}_raw` が既に存在すれば no-op (既に処理済み)
    - `result_dict[id_key]` が int でなければ no-op

    非int idを素通しするガードは、report_signal の context のような自由形式ペイロード
    (呼び出し元は {"type": ..., "id": ...} という形状だけで判定し、id の値の型までは
    保証しない) を経由する呼び出しで実際に踏まれるため維持している。

    Args:
        result_dict: 対象の dict (in-place 更新される)
        id_key: ID を保持するキー名 (デフォルト "id")
    """
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
