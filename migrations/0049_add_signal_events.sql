-- Migration 0049: signal_events テーブル追加
--
-- depends: 0048_session_identity
--
-- 背景:
--   cc-memory 自身の故障報告・使用感不満・矛盾検出・運用計測イベントの統一記録先。
--   decision / log と混ぜない理由: シグナルは「双方の合意」も文脈タグ体系も要らない
--   生の観測データであり、量が多く、状態遷移（トリアージ）を持つ。既存エンティティへ
--   の昇格は明示的な promote (status='promoted' + promoted_type/promoted_id) で行う。
--
--   本テーブルは体制全体のシグナル・運用計測イベントの唯一の記録先とする。他コンポ
--   ーネント（ops_metrics 集計・境界ゲートの shadow 突合ミラー）も本テーブルを共有し、
--   独自テーブルは作らない。
--
-- スキーマ:
--   id               主キー
--   kind             シグナル種別。妥当性は DB 制約ではなく Python 層の
--                    signal_service.KNOWN_KINDS で検証する（種別追加を migration 不要
--                    にするため）
--   source           発生源。'tool:<name>' / 'hook:<name>' / 'migration' / 'backup' /
--                    'agent' / 'user' / 'gate' 等の自由文字列
--   summary          1行要約
--   detail           traceback・引数ダイジェスト・自由記述（任意）
--   refs             JSON配列: [{"type":"decision","id":123}, ...]（任意）
--   context          JSON: kind ごとの構造化ペイロード（任意）
--   fingerprint      sha256(kind|source|正規化summary) 先頭16hex。dedup のキー
--   occurrence_count 同一fingerprintの再発回数
--   first_seen_at    初回記録時刻
--   last_seen_at     最終記録時刻（dedup 時に更新）
--   session_id       記録元セッションID（任意）
--   status           トリアージ状態。new → triaged / promoted / dismissed
--   promoted_type    promote先エンティティ種別（'topic'|'activity'|'decision'|'log'|'material'）
--   promoted_id      promote先エンティティID
--
-- dedup:
--   status='new' の行に限り fingerprint を UNIQUE とする部分インデックスを張る。
--   同一 fingerprint の new 行が既存なら INSERT は競合し、アプリ層は
--   ON CONFLICT(fingerprint) WHERE status='new' DO UPDATE で occurrence_count を
--   加算する（この部分 UNIQUE index 自体が並行書き込みの競合検知を担う）。
--   トリアージ済み（status が new 以外）の同型イベント再発は新規行になる。

CREATE TABLE signal_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,
    source           TEXT NOT NULL,
    summary          TEXT NOT NULL,
    detail           TEXT,
    refs             TEXT,
    context          TEXT,
    fingerprint      TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    session_id       TEXT,
    status           TEXT NOT NULL DEFAULT 'new'
                     CHECK (status IN ('new', 'triaged', 'promoted', 'dismissed')),
    promoted_type    TEXT,
    promoted_id      INTEGER,
    CHECK ((promoted_type IS NULL) = (promoted_id IS NULL))
);

CREATE UNIQUE INDEX idx_signal_fingerprint_new ON signal_events(fingerprint) WHERE status = 'new';
CREATE INDEX idx_signal_status ON signal_events(status, last_seen_at);
CREATE INDEX idx_signal_kind ON signal_events(kind, last_seen_at);
