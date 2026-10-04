"""UserPromptSubmitのpromptが人間の発話かどうかを判定する共有ロジック。

サブエージェントの報告の中継・他セッションからのメッセージ・バックグラウンド
タスクの完了通知・システム通知は、いずれもハーネスがpromptとして注入する
テキストであり、先頭ではなく「Another Claude session sent a message:」等の
前置きの後にタグが来る形で届く。先頭一致の正規表現では検出できないため、
本モジュールはprompt中のどこに現れてもマーカーを検出する。
"""
from __future__ import annotations

import re

_NONHUMAN_TURN_MARKERS = re.compile(
    "|".join(
        re.escape(marker)
        for marker in (
            "Another Claude session sent a message:",
            "<agent-message",
            "<cross-session-message",
            "<task-notification",
            "[SYSTEM NOTIFICATION",
        )
    )
)


def is_nonhuman_turn(prompt: object) -> bool:
    """promptが人間の発話でないターン（中継・通知）ならTrueを返す。

    add_askの回答通知はこの判定の対象外（呼び出し側がこの判定より先に
    処理する）。promptが文字列でない・空の場合はFalse。
    """
    if not isinstance(prompt, str) or not prompt:
        return False
    return _NONHUMAN_TURN_MARKERS.search(prompt) is not None
