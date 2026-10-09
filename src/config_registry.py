"""calmが読む環境変数の台帳。

名前・既定値・説明・種類を1か所に集約する。get_config がこの台帳と現在値を返し、
env-config スキルが利用者の調整対象（kind="user"）だけを見せる。README・man・
docs/setup.md の環境変数表はここを正として揃える。

kind:
  user       利用者が調整する値。env-configスキルの対象
  internal   内部の調整用・配備用の値。利用者は通常触らない
  emergency  不具合時に機能を止めるための緊急スイッチ
  session    セッションごとにプロセスが立てる値。settings.jsonに書くと全セッションに効いて困る
  ci         CI専用
"""
from __future__ import annotations

from typing import NamedTuple

from src.env_compat import env_get

KINDS = ("user", "internal", "emergency", "session", "ci")


class EnvVar(NamedTuple):
    name: str
    default: str | None  # 表示用。None は既定値なし（未設定が既定）
    description: str
    kind: str


ENV_VARS: tuple[EnvVar, ...] = (
    # --- user ---
    EnvVar("CALM_DB_PATH", "~/.claude/.claude-code-memory/discussion.db", "データベースファイルのパス", "user"),
    EnvVar("CALM_HEARTBEAT_TIMEOUT", "20", "ホットアクティビティ判定の閾値（分）", "user"),
    EnvVar("CALM_GOAL_RECHECK_HOURS", "6", "goalの担い手がhuman/externalのopen条件を「要確認」とみなすまでの時間（時間）", "user"),
    EnvVar("CALM_TIER2_MAX_AGE_DAYS", "7", "SessionStart一覧の階層2にin_progressアクティビティを載せるupdated_at上限（日）", "user"),
    EnvVar("CALM_TIER2_MAX_ITEMS", "5", "SessionStart一覧の『優先』に出す件数の上限", "user"),
    EnvVar("CALM_PIN_SURFACE_DECAY_DAYS", "60", "pinnedアクティビティが階層2表示を維持できるupdated_at上限（日）", "user"),
    EnvVar("CALM_RECENCY_DECAY_RATE", "0.0119", "検索の時間減衰率", "user"),
    EnvVar("CALM_PRECEDENT_BUDGET_CHARS", "24000", "pull_precedentsが本文展開（decision＋reason）に使う文字数予算", "user"),
    EnvVar("CALM_SYNC_DISABLE_RETROSPECTIVE", "false", "/sync-memoryのふりかえりセクションを非表示にする", "user"),
    EnvVar("CALM_SNAPSHOT_INTERVAL", "12", "スナップショット取得間隔（時間）", "user"),
    EnvVar("CALM_SNAPSHOT_MAX_COUNT", "5", "スナップショット最大保持数", "user"),
    EnvVar("CALM_SNAPSHOT_ANOMALY_THRESHOLD", "100", "行数減少の異常検知閾値（件）", "user"),
    EnvVar("CALM_SEARCH_HEALTH_WINDOW_DAYS", "7", "検索縮退・クエリ拡張停止検知の集計対象ウィンドウ（日）", "user"),
    EnvVar("CALM_SEARCH_HEALTH_MAX_SAMPLE", "100", "同集計で見る最大件数（timestamp降順）", "user"),
    EnvVar("CALM_SEARCH_HEALTH_MIN_SAMPLE", "20", "同集計の判定に必要な最小サンプル数（未満なら常に健全扱い）", "user"),
    EnvVar("CALM_SEARCH_HEALTH_DEGRADED_RATIO", "0.2", "検索の縮退率がこの値以上なら異常とみなす閾値", "user"),
    EnvVar("CALM_SEARCH_HEALTH_QE_FIRE_FLOOR", "0.0", "クエリ拡張の発火率がこの値以下なら異常とみなす閾値", "user"),
    EnvVar("CALM_PROJECTION_MANIFEST_MAX_ITEMS", "30", "intelligently habitsマニフェストの掲載件数上限", "user"),
    EnvVar(
        "CALM_PROJECT_ROOT",
        None,
        "embeddingサーバーを起動するプロジェクトルート。通常は自動解決されるため設定不要。"
        "自動解決に失敗する環境（gitリポジトリ外かつCLAUDE_PLUGIN_ROOTも未設定）でだけ明示する",
        "user",
    ),
    # --- internal: 検索・表示・予算の調整 ---
    EnvVar("CALM_SNOOZE_DURATION_DAYS", "3", "snoozeの既定期間（日）", "internal"),
    EnvVar("CALM_RECENCY_DECAY_FLOOR", "0.15", "検索のrecency boost下限", "internal"),
    EnvVar("CALM_RECENCY_DECAY_FLOOR_DECISION_LIVE", "0.7", "現役decisionのrecency boost下限", "internal"),
    EnvVar("CALM_ARCHIVED_DEMOTION_FACTOR", "0.3", "全タグがarchivedのアイテムのfinal_score降格係数", "internal"),
    EnvVar("CALM_ALWAYS_POOL_CAPACITY", "1500", "always層habitの定員（文字数）。2000未満に収める", "internal"),
    EnvVar("CALM_DIRECTION_OVERFLOW_THRESHOLD", "8", "domainごとの方向性decision件数がこの値以上でoverflow hintを出す", "internal"),
    EnvVar("CALM_HABIT_MANIFEST_DECAY_DAYS", "90", "intelligently層habitのマニフェスト表示から外れるまでの日数", "internal"),
    EnvVar("CALM_TAG_NOTES_DECAY_DAYS", "180", "tag notesの自動注入が1行ポインタに縮退するまでの日数", "internal"),
    EnvVar("CALM_HABITS_RULES_PATH", "~/.claude/rules/cc-memory-habits.md", "habits投影ファイルの書き込み先", "internal"),
    EnvVar("CALM_PEER_NUDGE_ENABLED", "0", "1で他セッションへの声かけ誘導を有効にする", "internal"),
    EnvVar("CALM_RELATED_RECORDS_BUDGET_CHARS", "600", "関連記録manifest添付の予算（文字数）", "internal"),
    EnvVar("CALM_RELATED_RECORDS_TOP_N", "3", "関連記録manifestの最大件数", "internal"),
    EnvVar("CALM_RELATED_RECORDS_TITLE_MAX_LEN", "40", "関連記録manifestのtitle最大文字数", "internal"),
    EnvVar("CALM_RELATED_RECORDS_SNIPPET_MAX_LEN", "120", "関連記録manifestのsnippet最大文字数", "internal"),
    EnvVar("CALM_RELATED_RECORDS_SIMILARITY_THRESHOLD", "0.65", "関連記録manifestの類似度足切り", "internal"),
    EnvVar("CALM_RELATED_RECORDS_CANDIDATE_LIMIT", "10", "関連記録manifestのKNN候補取得数", "internal"),
    EnvVar("CALM_INJECTION_BUDGET_SNAPSHOT", "1500", "SessionStart注入のスナップショット節の予算（文字数）", "internal"),
    EnvVar("CALM_INJECTION_BUDGET_ACTIVITIES", "4000", "SessionStart注入のアクティビティ節の予算（文字数）", "internal"),
    EnvVar("CALM_INJECTION_BUDGET_HABITS", "2500", "SessionStart注入のhabits節の予算（文字数）", "internal"),
    EnvVar("CALM_INJECTION_BUDGET_SIGNALS", "500", "SessionStart注入のシグナル節の予算（文字数）", "internal"),
    EnvVar("CALM_INJECTION_BUDGET_OPEN_ASKS", "1200", "SessionStart注入のopen ask節の予算（文字数）", "internal"),
    EnvVar("CALM_INJECTION_BUDGET_ASK_NOTIFY", "600", "SessionStart注入のask通知節の予算（文字数）", "internal"),
    EnvVar("CALM_INJECTION_BUDGET_TRANSCRIPT_PATH", "200", "SessionStart注入のtranscriptパス節の予算（文字数）", "internal"),
    EnvVar("CALM_INJECTION_BUDGET_VERSION_CHECK", "300", "SessionStart注入の版チェック節の予算（文字数）", "internal"),
    EnvVar("CALM_INJECTION_BUDGET_SEARCH_HEALTH", "300", "SessionStart注入の検索健全性節の予算（文字数）", "internal"),
    EnvVar("CALM_ACTIVITIES_BUDGET_CHARS", "10000", "get_activities応答全体の予算（文字数）", "internal"),
    EnvVar("CALM_TOTAL_INJECTION_BUDGET_CHARS", "12000", "SessionStart注入全体の上限（文字数）。各節の予算の合計以下にする", "internal"),
    EnvVar("CALM_PRECEDENT_ROUTING_CANDIDATES", "10", "pull_precedentsのtopic KNN候補数", "internal"),
    EnvVar("CALM_PRECEDENT_ROUTING_MISS_DISTANCE", "0.19", "pull_precedentsのrouting_miss判定のcosine距離閾値", "internal"),
    EnvVar("CALM_PRECEDENT_RESPONSE_CHARS_MAX", "32000", "pull_precedents応答全体の文字数上限", "internal"),
    EnvVar("CALM_CHECKIN_BUDGET_CHARS", "10000", "check_in応答全体の予算（文字数）", "internal"),
    EnvVar("CALM_CHECKIN_PINNED_SLOT_CHARS", "3000", "check_inのpinned専用枠（文字数）", "internal"),
    EnvVar("CALM_CHECKIN_CONTROL_CAP_CHARS", "3000", "check_inの制御信号枠の天井（文字数）", "internal"),
    EnvVar("CALM_CHECKIN_TAG_NOTES_CAP_CHARS", "6000", "check_inのtag notes枠の天井（文字数）", "internal"),
    EnvVar("CALM_CHECKIN_HARD_MAX_CHARS", "32000", "check_in応答の実用上限（文字数）", "internal"),
    # --- internal: embedding・launcher・セッション・配備 ---
    EnvVar("CALM_INSTALLED_PLUGINS_PATH", None, "installed_plugins.jsonのパス（未設定なら~/.claude/plugins/installed_plugins.json）", "internal"),
    EnvVar("CALM_EMBEDDING_BACKFILL_CHAR_BUDGET", "48000", "embedding backfill1回あたりの文字数予算", "internal"),
    EnvVar("CALM_EMBEDDING_BACKFILL_MAX_ITEMS", "64", "embedding backfill1回あたりの件数上限", "internal"),
    EnvVar("CALM_EMBEDDING_WARMUP", "1", "HTTPサーバー起動時にembeddingサーバーを先行起動する。0またはfalseで無効", "internal"),
    EnvVar("CALM_EMBEDDING_TEXT_MAX_CHARS", "8000", "embeddingに渡す1テキストの最大文字数", "internal"),
    EnvVar("CALM_EMBEDDING_LOG_MAX_BYTES", "5242880", "embeddingサーバーのログのローテーションサイズ（バイト）", "internal"),
    EnvVar("CALM_EMBEDDING_LOG_BACKUP_COUNT", "3", "embeddingサーバーのログの保持世代数", "internal"),
    EnvVar("CALM_LAUNCHER_HEARTBEAT_SEC", "60", "launcherのheartbeat間隔（秒）", "internal"),
    EnvVar("CALM_LAUNCHER_MAX_CONSECUTIVE_STREAM_EXCEPTIONS", "5", "launcherがストリーム例外の連続で諦める回数", "internal"),
    EnvVar("CALM_LAUNCHER_MAX_RETRIES", None, "launcherの起動リトライ上限。未設定なら既定の挙動", "internal"),
    EnvVar("CALM_LAUNCHER_STDIN_EOF_GRACE_SEC", "10", "launcherがstdin EOF後に待つ猶予（秒）", "internal"),
    EnvVar("CALM_AUTO_SHUTDOWN_SEC", None, "最後のセッションが消えてからサーバーを自動停止するまでの猶予（秒）。未設定なら自動停止しない", "internal"),
    EnvVar("CALM_STALENESS_CHECK_INTERVAL_SEC", "3600", "起動後にコードが更新されていないかを確認する間隔（秒）。0で陳腐化検知による自動再起動を無効化", "internal"),
    EnvVar("CALM_STALENESS_DEBOUNCE_SEC", "20", "更新を検知してから、再確認して自動再起動に進むまでの待ち（秒）", "internal"),
    EnvVar("CALM_HOLDER_WATCH_INTERVAL_SEC", "300", "orchの担い手欄の見張りが担い手の停止を確かめる間隔（秒）。0で見張りを無効化", "internal"),
    EnvVar("CALM_HOLDER_WATCH_DEAD_MIN", "10", "担い手のプロセスが無く、transcriptの最終更新からこの分数が過ぎたら停止として人に知らせる", "internal"),
    EnvVar("CALM_HOLDER_WATCH_STALE_MIN", "60", "担い手のプロセスはあるが、transcriptがこの分数更新されなければ停止として人に知らせる", "internal"),
    EnvVar("CALM_CLAUDE_PROJECTS_DIR", None, "Claude Codeのtranscript置き場（既定 ~/.claude/projects）", "internal"),
    EnvVar("CALM_SESSION_LIVENESS_TIMEOUT_SEC", "300", "heartbeatが途絶したセッションを失効させるまでの時間（秒）", "internal"),
    EnvVar("CALM_SESSION_REGISTRY_PATH", None, "セッション別名対応表のパス", "internal"),
    EnvVar("CALM_CLAUDE_SESSIONS_DIR", None, "Claude Codeのセッションディレクトリの場所", "internal"),
    EnvVar("CALM_ASK_NOTIFY_DIR", None, "ask通知ファイルの置き場", "internal"),
    EnvVar("CALM_HARNESS", None, "codexを指定するとCodexハーネス向けの入出力になる", "internal"),
    EnvVar("CALM_URL", None, "指定すると、launcherがローカルではなくこのリモートサーバーへ接続する", "internal"),
    EnvVar("CALM_REMOTE_PORT", "8001", "リモートサーバーのポート", "internal"),
    EnvVar("CALM_BASE_URL", None, "リモートサーバーの公開URL（リモート起動時に必須）", "internal"),
    EnvVar("CALM_ALLOWED_USERS", None, "リモートサーバーに入れるGitHubユーザー（リモート起動時に必須）", "internal"),
    EnvVar("CALM_PENDING_DIR", None, "bg依頼文ラッパーが、CALMに書けない内容を退避する場所", "internal"),
    # --- emergency ---
    EnvVar("CALM_HABITS_RULES_EXPORT", "1", "0でhabits投影を止める。止めると投影済みファイルはプレースホルダのまま更新されない", "emergency"),
    EnvVar("CALM_MIGRATION_SNAPSHOT", "1", "0でマイグレーション前のスナップショット取得を止める", "emergency"),
    EnvVar("CALM_MIGRATION_DRYRUN", "1", "0でマイグレーションの実DBコピーdry-run適用を止める", "emergency"),
    EnvVar("CALM_MIGRATION_HASH_ENFORCE", "error", "マイグレーション内容ハッシュ不一致時の動作。error=起動中断、warn=警告のみで続行", "emergency"),
    EnvVar("CALM_LEAK_GUARD", None, "offで内部ID漏出防止hookのブロックを止める", "emergency"),
    EnvVar("CALM_SANITIZE_DISABLE", None, "1でtool result・transcriptのsanitize hookを止める", "emergency"),
    # --- session ---
    EnvVar("CALM_RECORDER", None, "記録担当セッションに立つ印。settings.jsonのenvに書くと全セッションが記録担当になる", "session"),
    # --- ci ---
    EnvVar("CALM_PR_BODY", None, "CIのlintがPR本文を受け取るための値", "ci"),
)


def list_env_vars(kind: str | None = "user") -> list[dict]:
    """台帳と現在値を返す。kind=None で全種類。

    value は環境変数の現在値（旧名フォールバックを含む）。未設定なら None。
    """
    return [
        {
            "name": v.name,
            "kind": v.kind,
            "default": v.default,
            "description": v.description,
            "value": env_get(v.name),
        }
        for v in ENV_VARS
        if kind is None or v.kind == kind
    ]
