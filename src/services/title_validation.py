"""title 引数の長さ validation helper。

cc-memory の add/update ツール群で title 引数を TITLE_MAX_LEN 字以内に揃える。
docstring 明記と validation の両輪で AI 側に制約を伝える。
"""

TITLE_MAX_LEN = 40


def validate_title(title: str | None) -> dict | None:
    """title の長さを validate する。

    title が TITLE_MAX_LEN 字超なら VALIDATION_ERROR を返す。
    None は検証 skip (update 系で未指定 / add_decisions items の空 title 等)。

    Args:
        title: 検証対象。None なら検証 skip。

    Returns:
        validation エラー時は error dict、OK なら None。
    """
    if title is not None and len(title) > TITLE_MAX_LEN:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": (
                    f"title length {len(title)} exceeds maximum {TITLE_MAX_LEN}"
                ),
            }
        }
    return None
