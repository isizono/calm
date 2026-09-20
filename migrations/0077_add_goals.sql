-- Migration 0077: goal機構（goals / goal_conditions / goal_activities）と
--   activitiesの終了記録列を追加
--
-- depends: 0074_drop_relay_outbox
--
-- 背景:
--   activityが目指す終了状態（goal）を、真偽の付く条件の集合として表現し、
--   全条件が終端（satisfied/waived）したことをサーバーが機械的に検出し、
--   閉じるのは明示判定（judge_goal）だけにする機構を導入する。
--
-- スキーマ:
--   goals            goal本体。handleとstatementのみを持ち、判定記録
--                    （verdict/judged_by/judged_at/judge_note）は最後の1回分を
--                    差し戻し（closed=0への書き戻し）でも消さずに残す。
--   goal_conditions  条件。状態を保存する単位。文は作成後に変えない
--                    （書き換えはwaiveと新規追加で表す）。束縛はactivity/decision/askの
--                    いずれか1件を指す多相参照で、FKは張らない（束縛先の実在は
--                    アプリ層で確認する）。
--   goal_activities  activityとgoalの紐づけ、または不要印。activityごとに高々1行。
--                    WITHOUT ROWIDにし、activity_idをrowidの別名にしない
--                    （rowidの別名だと、activity_idを省いたINSERTが自動採番で
--                    通ってしまうため）。
--
-- 変更内容:
--   1. goals / goal_conditions / goal_activities を新設
--   2. activitiesにclosed_at・closed_by・closed_reasonを追加（NULL許容。
--      closed_byのCHECKがclosed_atを参照するため、closed_atを先に追加する）

CREATE TABLE goals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    handle      TEXT NOT NULL UNIQUE
                CHECK (LENGTH(handle) > 0 AND handle NOT GLOB '*[^a-z0-9-]*'),
    statement   TEXT NOT NULL CHECK (LENGTH(TRIM(statement)) > 0),
    closed      INTEGER NOT NULL DEFAULT 0 CHECK (closed IN (0, 1)),
    verdict     TEXT CHECK (verdict IS NULL OR verdict IN ('achieved', 'failed')),
    judged_by   TEXT CHECK (judged_by IS NULL OR judged_by IN ('session', 'human')),
    judged_at   TIMESTAMP,
    judge_note  TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((verdict IS NULL) = (judged_at IS NULL) AND (verdict IS NULL) = (judged_by IS NULL)),
    CHECK (verdict IS NULL OR verdict <> 'failed' OR LENGTH(TRIM(COALESCE(judge_note, ''))) > 0),
    CHECK (closed = 0 OR verdict IS NOT NULL)
);

CREATE TABLE goal_conditions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id           INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    statement         TEXT NOT NULL CHECK (LENGTH(TRIM(statement)) > 0),
    actor             TEXT NOT NULL CHECK (actor IN ('claude', 'human', 'external')),
    state             TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'satisfied', 'waived')),
    note              TEXT,
    last_satisfied_at TIMESTAMP,
    bound_type        TEXT CHECK (bound_type IS NULL OR bound_type IN ('activity', 'decision', 'ask')),
    bound_id          INTEGER,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (state <> 'satisfied' OR last_satisfied_at IS NOT NULL),
    CHECK (state <> 'waived' OR LENGTH(TRIM(COALESCE(note, ''))) > 0),
    CHECK ((bound_type IS NULL) = (bound_id IS NULL))
);
CREATE INDEX idx_goal_conditions_goal ON goal_conditions(goal_id);

CREATE TABLE goal_activities (
    activity_id    INTEGER NOT NULL PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE,
    goal_id        INTEGER REFERENCES goals(id) ON DELETE CASCADE,
    waiver_reason  TEXT,
    added_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((goal_id IS NULL) <> (waiver_reason IS NULL)),
    CHECK (waiver_reason IS NULL OR LENGTH(TRIM(waiver_reason)) > 0)
) WITHOUT ROWID;
CREATE INDEX idx_goal_activities_goal ON goal_activities(goal_id);

ALTER TABLE activities ADD COLUMN closed_at TIMESTAMP DEFAULT NULL;
ALTER TABLE activities ADD COLUMN closed_by TEXT DEFAULT NULL
    CHECK (closed_by IS NULL
           OR (closed_by IN ('goal_judge', 'user', 'claude', 'external') AND closed_at IS NOT NULL));
ALTER TABLE activities ADD COLUMN closed_reason TEXT DEFAULT NULL;
