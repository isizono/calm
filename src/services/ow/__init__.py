"""ow runtime state cache layer.

真実源は relay events。本パッケージが管理する JSON ファイルキャッシュは
relay から再生成可能な派生データに過ぎず、破損・schema mismatch時には
削除して呼び出し側に full pull を促す。
"""

from src.services.ow.cache import (
    CURRENT_SCHEMA_VERSION,
    OwState,
    find_topic_id_by_channel,
    load_state,
    save_state,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "OwState",
    "find_topic_id_by_channel",
    "load_state",
    "save_state",
]
