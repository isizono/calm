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
# SessionStart一覧の階層2（優先）に in_progress アクティビティを載せる updated_at 上限（日）
TIER2_MAX_AGE_DAYS: int = int(os.environ.get("CCM_TIER2_MAX_AGE_DAYS", "7"))
# pinned アクティビティが階層2表示を維持できる updated_at 上限（日）。
# 超過すると階層2から外れ固定ナビの未表示件数句に計上される（pin自体は残る）
PIN_SURFACE_DECAY_DAYS: int = int(os.environ.get("CCM_PIN_SURFACE_DECAY_DAYS", "60"))

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

# --- Migration Safety ---
# premigration スナップショット取得を無効化する緊急脱出弁（"0" で無効化）
CCM_MIGRATION_SNAPSHOT: bool = os.environ.get("CCM_MIGRATION_SNAPSHOT", "1") != "0"
# 実DBコピーへのdry-run適用ゲートを無効化する緊急脱出弁（"0" で無効化）
CCM_MIGRATION_DRYRUN: bool = os.environ.get("CCM_MIGRATION_DRYRUN", "1") != "0"
# migration_ledger内容ハッシュ不一致時の既定動作。"error"（既定、起動中断）| "warn"（警告のみで続行）
CCM_MIGRATION_HASH_ENFORCE: str = os.environ.get("CCM_MIGRATION_HASH_ENFORCE", "error").lower()

# --- Precedent pull ---
# 本文展開（decision + reason）の予算（文字数）。index行・material snippet・routing
# メタデータは対象外（別途有界のため予算計算に含めない）
PRECEDENT_BUDGET_CHARS: int = int(os.environ.get("CCM_PRECEDENT_BUDGET_CHARS", "24000"))
PRECEDENT_ROUTING_K_MAX: int = 5
# topic_vec KNNで取得する候補数（selected上限のPRECEDENT_ROUTING_K_MAXより広めに取る）
PRECEDENT_ROUTING_CANDIDATES: int = int(os.environ.get("CCM_PRECEDENT_ROUTING_CANDIDATES", "10"))
# topic_vecはdistance_metric=cosineで作成されるため、この閾値もcosineスケール
# （0=完全一致、1=無相関）で解釈する。実DBコピー（decision reason本文を検索クエリ、
# 所属topicを正解として使う実測）では、正解topicへの距離は中央値0.19付近、
# 無関係topicへの距離は中央値0.22付近で重なりが大きく、単一閾値による分離力は
# 強くない。閾値を上げるほど正解の取りこぼし（miss）は減るが無関係topicの
# 誤選定も増えるため、本閾値は「無関係topicを誤ってselected扱いする率を1割前後に
# 抑える」側に倒した値（正解の取りこぼし率は実測で5割弱）。運用データで再調整する
# 前提の初期値であり、0.6のような緩い値は実測上ほぼ全topicが閾値内に入ってしまい
# routing_missが機能しなくなるため使わない。
PRECEDENT_ROUTING_MISS_DISTANCE: float = float(os.environ.get("CCM_PRECEDENT_ROUTING_MISS_DISTANCE", "0.19"))
