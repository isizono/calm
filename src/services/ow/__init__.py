"""ow runtime state cache layer.

本パッケージが管理する JSON ファイルキャッシュは relay events から再生成可能な
派生データに過ぎず、真実源ではない (耐久的事実の真実源は cc-memory、relay は
bounded transport buffer で liveness の直近窓のみを担う)。破損・schema mismatch
時には削除して呼び出し側に full pull を促す。
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
