"""cc-memory 設定モジュール。環境変数で定数をオーバーライド可能にする。"""
import os
from pathlib import Path

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

# --- Habits ---
# always層（常時注入枠）の定員（文字数）。update_habitでtrigger_mode='always'に
# 昇格する際、昇格後のプール合計文字数がこの値と昇格前合計の大きい方を超えると
# 拒否する（プールが定員超過中でも、合計を増やさない変更は許可するラチェット）
ALWAYS_POOL_CAPACITY: int = int(os.environ.get("CCM_ALWAYS_POOL_CAPACITY", "1500"))

# habits DBから ~/.claude/rules 配下へ投影する自動生成ファイルの書き込み先パス
HABITS_RULES_PATH: str = os.environ.get("CCM_HABITS_RULES_PATH") or str(
    Path.home() / ".claude" / "rules" / "cc-memory-habits.md"
)
# 投影のkill switch。"0"で無効化すると、以後の投影はプレースホルダ本文で
# 上書きされたまま停止する（stale化したファイルが注入され続けるのを防ぐ）
HABITS_RULES_EXPORT_ENABLED: bool = os.environ.get("CCM_HABITS_RULES_EXPORT", "1") != "0"
# intelligently層マニフェストの独立予算（件数）。importance_score降順で選抜し、
# 超過分は本文を切断せず件数行1行に縮退する
PROJECTION_MANIFEST_MAX_ITEMS: int = int(
    os.environ.get("CCM_PROJECTION_MANIFEST_MAX_ITEMS", "30")
)

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

# --- Relay session awareness ---
# relay Monitor監視指示のopt-in kill switch。デフォルトOFF（"1"でON）。
# OFF時はSessionStartからrelay関連の文言を一切出さず、relayを使わない
# ユーザー・セッションにコンテキストを注入しない。
RELAY_SESSION_AWARE_ENABLED: bool = os.environ.get("CCM_RELAY_SESSION_AWARE", "0") == "1"

# --- Archived tags ---
# 全タグがarchivedのアイテムに適用する final_score の降格係数
ARCHIVED_DEMOTION_FACTOR: float = float(os.environ.get("CCM_ARCHIVED_DEMOTION_FACTOR", "0.3"))

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
# レスポンス全体（JSON文字列化後）の実測文字数上限。PRECEDENT_BUDGET_CHARSは
# decision本文（decision+reason）のみを計上するが、full itemにはtags/sections/
# supersede_chain/material_idsが、レスポンス全体にはmaterialカタログやindex行群も
# 乗るため、本文予算内でも実サイズはその数倍になり得る。MCPツール結果の実用上限
# （約2.5万トークン）に対する日本語主体レスポンスの文字/トークン比の安全側見積もり
# から逆算した値
PRECEDENT_RESPONSE_CHARS_MAX: int = int(os.environ.get("CCM_PRECEDENT_RESPONSE_CHARS_MAX", "32000"))
