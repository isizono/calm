-- Migration 0062: asks（判断委譲の受け皿）テーブル追加
--
-- depends: 0061_add_tag_archived
--
-- 背景:
--   AIエージェントが人間の判断を待つ問いを1箇所に積み、人間が回答するだけで
--   作業を再開できるようにする受け皿。signal_events（migration 0049）と同様、
--   合意不要の生の観測データを専用テーブルに記録する設計だが、状態遷移
--   （open→answered→promoted/dismissed、およびopen→withdrawn）を持つため
--   signal_eventsとはテーブルを共有せず独立させる。
--
-- スキーマ:
--   asks             本体。1問1答モデル（answerは1回のみ）。
--                    トリアージ（promote/dismiss）はanswer時点では行わず、
--                    次のcheck_inで配達されるまで遅延する。
--   ask_blocks       このaskが答え待ちで止めているactivityのjunction。
--   ask_requesters   このaskを要求したセッションのUNION蓄積。
--   ask_vec          question本文のembeddingによる類似ask検索用
--                    （topic_vecと同型のask専用vec0仮想テーブル）。
--
-- dedup:
--   同一question（正規化後）でstatus='open'の行が既存なら、部分UNIQUEインデックス
--   （fingerprint WHERE status='open'）が競合を検知し、アプリ層は新規行を作らず
--   occurrence_countを加算する。answered/promoted/dismissed/withdrawnの同一questionは
--   別のライフとして新規行になる。

CREATE TABLE asks (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    question               TEXT NOT NULL,
    context                TEXT,
    fingerprint            TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'open'
                           CHECK (status IN ('open','answered','promoted','dismissed','withdrawn')),

    answer_body            TEXT,
    answered_at            TIMESTAMP NULL,
    answered_session_id    TEXT,

    triage                 TEXT
                           CHECK (triage IS NULL OR triage IN ('promote','dismiss')),
    triaged_at             TIMESTAMP NULL,
    triaged_session_id     TEXT,
    triage_reason          TEXT,
    promoted_decision_id   INTEGER,

    withdrawn_at           TIMESTAMP NULL,
    withdrawn_session_id   TEXT,
    withdraw_reason        TEXT,

    occurrence_count       INTEGER NOT NULL DEFAULT 1,
    first_seen_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    first_seen_session_id  TEXT,
    last_seen_session_id   TEXT,

    CHECK (
        (status IN ('open','withdrawn'))
        OR (answer_body IS NOT NULL AND answered_at IS NOT NULL)
    ),
    CHECK (
        (triage IS NULL) OR (answered_at IS NOT NULL)
    ),
    CHECK (
        (status = 'promoted' AND promoted_decision_id IS NOT NULL)
        OR (status <> 'promoted' AND promoted_decision_id IS NULL)
    ),
    CHECK (
        (status = 'withdrawn' AND withdrawn_at IS NOT NULL)
        OR (status <> 'withdrawn' AND withdrawn_at IS NULL)
    ),

    FOREIGN KEY (promoted_decision_id) REFERENCES decisions(id)
);

CREATE UNIQUE INDEX idx_asks_fingerprint_open
    ON asks(fingerprint) WHERE status = 'open';

CREATE INDEX idx_asks_status_last_seen
    ON asks(status, last_seen_at);

CREATE INDEX idx_asks_triage_pending
    ON asks(last_seen_at)
    WHERE status = 'answered' AND triage IS NULL;

CREATE TABLE ask_blocks (
    ask_id      INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
    added_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ask_id, activity_id),
    FOREIGN KEY (ask_id) REFERENCES asks(id) ON DELETE CASCADE,
    FOREIGN KEY (activity_id) REFERENCES activities(id) ON DELETE CASCADE
);

CREATE INDEX idx_ask_blocks_activity ON ask_blocks(activity_id, ask_id);

CREATE TABLE ask_requesters (
    ask_id                INTEGER NOT NULL,
    requester_session_id  TEXT NOT NULL,
    added_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ask_id, requester_session_id),
    FOREIGN KEY (ask_id) REFERENCES asks(id) ON DELETE CASCADE
);

-- rowid = asks.id（topic_vecと同じくask専用のためsource_id直用）。
-- distance_metric=cosineは埋め込みが非正規化のため、topic_vecと同じ理由で明示する。
-- 仮想テーブルのため外部キー制約が使えない。askを物理削除する経路を追加する場合は
-- 同一トランザクションでask_vecの対応行も削除すること。
CREATE VIRTUAL TABLE IF NOT EXISTS ask_vec USING vec0(
  embedding float[384] distance_metric=cosine
);
