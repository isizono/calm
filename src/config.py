"""cc-memory 設定モジュール。環境変数で定数をオーバーライド可能にする。"""
import os

# --- Database ---
# CCM_DB_PATH を優先、なければ既存の DISCUSSION_DB_PATH にフォールバック
DB_PATH: str | None = os.environ.get("CCM_DB_PATH") or os.environ.get("DISCUSSION_DB_PATH")

# --- Activity ---
HEARTBEAT_TIMEOUT_MINUTES: int = int(os.environ.get("CCM_HEARTBEAT_TIMEOUT", "20"))
SNOOZE_DURATION_DAYS: int = int(os.environ.get("CCM_SNOOZE_DURATION_DAYS", "3"))

# --- Active Context 表示 ---
IN_PROGRESS_LIMIT: int = int(os.environ.get("CCM_IN_PROGRESS_LIMIT", "3"))
PENDING_LIMIT: int = int(os.environ.get("CCM_PENDING_LIMIT", "2"))

# --- Search ---
# Recency boost の減衰率（指数減衰 e^(-kt)）。30日で約0.70倍、半減期約58日
RECENCY_DECAY_RATE: float = float(os.environ.get("CCM_RECENCY_DECAY_RATE", "0.0119"))
# Recency boost の下限。約160日以降はこの値で一定になる
RECENCY_DECAY_FLOOR: float = float(os.environ.get("CCM_RECENCY_DECAY_FLOOR", "0.15"))

# --- Snapshot ---
SNAPSHOT_INTERVAL_HOURS: int = int(os.environ.get("CCM_SNAPSHOT_INTERVAL", "12"))
SNAPSHOT_MAX_COUNT: int = int(os.environ.get("CCM_SNAPSHOT_MAX_COUNT", "5"))
SNAPSHOT_ANOMALY_THRESHOLD: int = int(os.environ.get("CCM_SNAPSHOT_ANOMALY_THRESHOLD", "100"))

# --- Sync Memory ---
SYNC_DISABLE_RETROSPECTIVE: bool = os.environ.get(
    "CCM_SYNC_DISABLE_RETROSPECTIVE", "false"
).lower() in ("true", "1")
SYNC_POLICY: str | None = os.environ.get("CCM_SYNC_POLICY") or None  # 空文字→None正規化

# --- Direction Layer ---
# domainごとのactiveな方向性decision(layer:direction)件数がこの値以上になったら
# direction_overflow hintを発火する（少数原則の維持を促す）
DIRECTION_OVERFLOW_THRESHOLD: int = int(os.environ.get("CCM_DIRECTION_OVERFLOW_THRESHOLD", "8"))
