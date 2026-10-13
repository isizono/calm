-- Migration 0091: destination_telemetry（宛先候補の算出ログ）新設
--
-- depends: 0089_sessions_add_stale_on_startup_reason
--
-- 背景:
--   宛先候補（📮）は算出されても0件で何も出ない場合が大半で、「何回算出して、
--   なぜ出なかったか」が事後に分からない。算出のたびに1行残し、早期returnの
--   理由まで追えるようにする。
--
-- スキーマ:
--   caller_session_id : 算出を起こしたセッションの相関キー（NULL許容）
--   trigger_tool       : 算出を起こしたツール名（check_in / add_logs 等）
--   path               : 'goal'（判定待ち）| 'board'（掲示板投稿）| 'nearby'（check_in近傍）
--   candidate_count    : 応答に出した候補数。出さなかった場合は0
--   reason             : 'injected' または出さなかった理由（no_candidates 等）
--
--   FK・UNIQUE制約は張らない（既存telemetryテーブル群と同じ生データ台帳の方針）。

CREATE TABLE destination_telemetry (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_session_id  TEXT,
    trigger_tool       TEXT NOT NULL,
    path               TEXT NOT NULL,
    candidate_count    INTEGER NOT NULL,
    reason             TEXT NOT NULL,
    timestamp          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_destination_telemetry_timestamp
    ON destination_telemetry(timestamp);
