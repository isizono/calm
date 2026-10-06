-- Migration 0090: フィードバックエントリにtiming='output'(Claudeの出力文への照合)を追加
--
-- depends: 0089_sessions_add_stale_on_startup_reason
-- destructive: feedback_entriesのCHECK制約更新のため新テーブルへ全行を写したうえで旧テーブルをDROP TABLEする（行・id・子テーブルの参照は保たれる）
--
-- 背景:
--   Claudeが直前に書いた文(assistantのtextブロック)に特定の語があれば、次の
--   UserPromptSubmitで一言のヒントを届ける。照合の既読位置と、同じエントリの
--   自己発火を抑えるクールダウンをセッションごとに持つ。
--
-- 変更内容:
--   1. feedback_entries.timingのCHECKに'output'を足す（CHECK値一覧の変更には
--      テーブル再作成が必要。子テーブルのFKは名前で解決されるため、新テーブルを
--      作ってデータを写し、旧テーブルを落として改名する）
--   2. feedback_output_cursor: セッションごとのtranscript既読位置(byte_offset)と
--      UserPromptSubmitの通し番号。transcriptの本文は保存しない
--   3. feedback_output_cooldowns: セッション×エントリごとの直近配達時の通し番号

PRAGMA defer_foreign_keys = ON;

CREATE TABLE feedback_entries_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE
                      CHECK (LENGTH(name) > 0 AND name NOT GLOB '*[^a-z0-9-]*'),
    body              TEXT NOT NULL CHECK (LENGTH(body) <= 100 AND LENGTH(TRIM(body)) > 0),
    ref               TEXT CHECK (ref IS NULL OR LENGTH(ref) <= 500),
    strength          TEXT NOT NULL CHECK (strength IN ('notify', 'block')),
    timing            TEXT NOT NULL CHECK (timing IN ('utterance', 'tool_fail', 'pre_tool', 'output')),
    -- {"tool": str|null, "all": [{"field","op","value"}, ...]}（all は0〜3要素）。
    -- 実行時の評価規則は src/services/feedback_rules.py 参照
    condition_json    TEXT NOT NULL,
    delivered_count   INTEGER NOT NULL DEFAULT 0,
    overridden_count  INTEGER NOT NULL DEFAULT 0,
    deleted_at        TIMESTAMP,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- strength='block' は実行直前ブロック以外に配達経路を持たないため、
    -- timing='pre_tool' と常に対応させる（0079と同じ）
    CHECK ((strength = 'block') = (timing = 'pre_tool'))
);

INSERT INTO feedback_entries_new (
    id, name, body, ref, strength, timing, condition_json,
    delivered_count, overridden_count, deleted_at, created_at, updated_at
)
SELECT
    id, name, body, ref, strength, timing, condition_json,
    delivered_count, overridden_count, deleted_at, created_at, updated_at
FROM feedback_entries;

DROP TABLE feedback_entries;
ALTER TABLE feedback_entries_new RENAME TO feedback_entries;

CREATE TABLE feedback_output_cursor (
    session_id   TEXT PRIMARY KEY,
    byte_offset  INTEGER NOT NULL DEFAULT 0,
    turn_seq     INTEGER NOT NULL DEFAULT 0,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE feedback_output_cooldowns (
    session_id     TEXT NOT NULL,
    entry_id       INTEGER NOT NULL REFERENCES feedback_entries(id),
    last_turn_seq  INTEGER NOT NULL,
    PRIMARY KEY (session_id, entry_id)
);
