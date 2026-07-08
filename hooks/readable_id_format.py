"""activity 表示用の readable ID 整形 helper

session_start_hook の activity 一覧表示で、整数 ID を `{title} (#NNN)` 形式の
文字列に整形するために使う。タイトル + (#NNN) 形式の文字列展開は tool レスポンス
整形経路 (id 削除 + id_raw 退避、src/services/readable_id.py) では廃止済みで、
本 helper はそれとは別経路の hook 専用実装であり、新規 caller を増やさないこと。
"""


def format_readable_id(id_int: int, title: str | None) -> str:
    """エンティティ ID を `{title} (#NNN)` 形式の文字列で返す。

    Args:
        id_int: 元の整数 ID
        title: エンティティのタイトル (None や空文字列の場合は ID のみ)

    Returns:
        `{title} (#{id_int})` 形式の文字列。
        title が None または空文字列の場合は `(#{id_int})` のみ。
    """
    if title:
        return f"{title} (#{id_int})"
    return f"(#{id_int})"
