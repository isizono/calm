-- Migration 040: ow_channels / ow_workers / ow_applied_msg_ids テーブル追加
--
-- depends: 0039_extend_tag_namespace
--
-- 背景:
--   旧queue-t<topic_id>.md（ファイルベースのworker queue管理）を廃止し、
--   cc-memory本体DBにowランタイム状態を集約する設計（T53）の Phase 1 基盤。
--   relay履歴を権威ソースとした event-sourcing reducer が書き込む派生ビュー(MV)で、
--   1) ow_channels: relayチャネルとtopic/orchの紐づけ
--   2) ow_workers: worker run instance（再spawn可能、履歴付き）
--   3) ow_applied_msg_ids: reducer idempotency 用
--
--   ユーザー判断:
--   - session_id は NULL 許容（identity未受信のready前workerもreducerで表現可能にするため）
--   - workload_state / cause / outcome は CHECK 制約 enum 化
--   - 1 worker = 1 activity の物理強制は部分 UNIQUE INDEX で実現
--
-- 変更内容:
--   - ow_channels テーブル新規追加
--   - ow_workers テーブル新規追加 + 部分UNIQUE INDEX 3つ + 通常INDEX 3つ
--   - ow_applied_msg_ids テーブル新規追加

-- ============================================
-- ow_channels: relayチャネルとtopic/orchの紐づけ
-- ============================================
CREATE TABLE ow_channels (
    channel_code         TEXT PRIMARY KEY,
    topic_id             INTEGER NOT NULL,
    orch_handle          TEXT NOT NULL,
    orch_activity_id     INTEGER,
    orch_cwd             TEXT,
    orch_session_id      TEXT,
    last_seen_msg_id     INTEGER NOT NULL DEFAULT 0,
    deleted_at           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    FOREIGN KEY (topic_id) REFERENCES discussion_topics(id),
    FOREIGN KEY (orch_activity_id) REFERENCES activities(id) ON DELETE SET NULL
);

CREATE INDEX idx_ow_channels_topic ON ow_channels(topic_id);

-- ============================================
-- ow_workers: workerランタイム状態 MV
-- ============================================
CREATE TABLE ow_workers (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_code         TEXT NOT NULL,
    handle               TEXT NOT NULL,
    alias                TEXT NOT NULL,
    activity_id          INTEGER,
    topic_id             INTEGER NOT NULL,
    task_n               INTEGER NOT NULL,
    model                TEXT,
    cwd                  TEXT,
    permission_mode      TEXT,
    timeout_min          INTEGER,
    task_material_id     INTEGER,
    session_id           TEXT,
    workload_state       TEXT NOT NULL DEFAULT 'spawning' CHECK(workload_state IN
                          ('spawning','loading','ready','working','blocked','escalated','draining','terminated')),
    cause                TEXT CHECK(cause IS NULL OR cause IN
                          ('closed','cancelled','dead','crashed','crashed-during-drain')),
    last_state_msg_id    INTEGER,
    last_heartbeat_at    TEXT,
    spawned_at           TEXT NOT NULL,
    ready_at             TEXT,
    terminated_at        TEXT,
    FOREIGN KEY (channel_code) REFERENCES ow_channels(channel_code),
    FOREIGN KEY (activity_id) REFERENCES activities(id) ON DELETE SET NULL,
    FOREIGN KEY (topic_id) REFERENCES discussion_topics(id),
    FOREIGN KEY (task_material_id) REFERENCES materials(id) ON DELETE SET NULL
);

-- alive worker の handle 単位一意（再spawnで terminated は履歴として残せる）
CREATE UNIQUE INDEX uq_ow_workers_alive_handle
    ON ow_workers(channel_code, handle) WHERE workload_state != 'terminated';

-- 1 worker = 1 activity の物理強制（活動中のみ）
CREATE UNIQUE INDEX uq_ow_workers_one_alive_per_activity
    ON ow_workers(activity_id) WHERE workload_state != 'terminated' AND activity_id IS NOT NULL;

-- channel単位の task_n 連番一意
CREATE UNIQUE INDEX uq_ow_workers_task_n ON ow_workers(channel_code, task_n);

CREATE INDEX idx_ow_workers_activity ON ow_workers(activity_id);
CREATE INDEX idx_ow_workers_state ON ow_workers(channel_code, workload_state);
CREATE INDEX idx_ow_workers_alive ON ow_workers(channel_code) WHERE workload_state != 'terminated';

-- ============================================
-- ow_applied_msg_ids: reducer idempotency
-- ============================================
CREATE TABLE ow_applied_msg_ids (
    channel_code         TEXT NOT NULL,
    msg_id               INTEGER NOT NULL,
    applied_at           TEXT NOT NULL,
    outcome              TEXT NOT NULL CHECK(outcome IN ('applied','skipped')),
    PRIMARY KEY (channel_code, msg_id)
);
