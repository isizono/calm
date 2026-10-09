-- Migration 0093: 担い手欄を差し替えた直後の、起こし直し(CronCreate)の仕込みの確かめ待ちを持つ
--
-- depends: 0092_feedback_output_timing
--
-- 背景:
--   担い手欄だけを差し替える部分更新では、後継がまだ仕込んでいないのが普通なので、
--   起こし直しのjob id検査を差し替えの直後に出さず、サーバーの見張りが猶予の後に
--   確かめる。待ちをプロセス内に持つとサーバーの再起動やリモートサーバー経由の呼び出しで
--   検査が消えるため、DBに持つ。
--
-- 変更内容:
--   holder_cron_pending: 差し替えた時刻と、差し替え前の説明(job idの差を取る基準)を
--   activity×新担い手のsessionIdごとに持つ。見張りが確かめ終えたら消す

CREATE TABLE holder_cron_pending (
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    replaced_at REAL NOT NULL,
    base_description TEXT NOT NULL,
    PRIMARY KEY (activity_id, session_id)
);
