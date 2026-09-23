-- Migration 0079: フィードバック機構（feedback_entries等6テーブル）を追加
--
-- depends: 0077_add_goals
--
-- 背景:
--   Claudeが同じところで躓き続けるのを防ぐため、Claude自身が知見を書き残し、
--   発話・ツール失敗・ツール実行直前の3タイミングでその知見を自分に配達する
--   仕組みを追加する。エントリはClaudeが自由に付け消し変更してよい（ユーザーが
--   設定するsettings/rules/habitsとは別の層）。
--
-- スキーマ:
--   feedback_entries        エントリ本体。名前で引く。strength='block'のエントリは
--                            必ずtiming='pre_tool'（実行直前ブロック以外の配達経路を
--                            持たないため、両者は双方向対応にする）
--   feedback_notes           エントリごとの追記専用ノート（躓き観測・経緯）。
--                            UPDATE/DELETEはトリガーで拒否する
--   feedback_holds            1回止め（block強度）の保留。session_id×entry_idにつき
--                            最新1件のみ持つ。次回同じ指紋の呼び出しが来たら
--                            ブロックせず通す
--   feedback_turn_marks       「同じエントリは区切り(prompt_id)ごとに1回だけ配達」の
--                            重複配達防止マーカー
--   feedback_bootstrap_seen  「ツール失敗で当たるエントリが無かった」ときのセッション
--                            1回リマインド済みmarker
--   feedback_switch          配達の停止スイッチ（id=1固定の単一行）
--
-- 変更内容:
--   1. feedback_entries / feedback_notes / feedback_holds / feedback_turn_marks /
--      feedback_bootstrap_seen / feedback_switch を新設
--   2. feedback_notesへのUPDATE/DELETEを拒否する追記専用トリガーを追加
--   3. feedback_switchへの初期行（mode='on'）を投入

CREATE TABLE feedback_entries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE
                      CHECK (LENGTH(name) > 0 AND name NOT GLOB '*[^a-z0-9-]*'),
    body              TEXT NOT NULL CHECK (LENGTH(body) <= 100 AND LENGTH(TRIM(body)) > 0),
    ref               TEXT CHECK (ref IS NULL OR LENGTH(ref) <= 500),
    strength          TEXT NOT NULL CHECK (strength IN ('notify', 'block')),
    timing            TEXT NOT NULL CHECK (timing IN ('utterance', 'tool_fail', 'pre_tool')),
    -- {"tool": str|null, "all": [{"field","op","value"}, ...]}（all は0〜3要素）。
    -- 実行時の評価規則は src/services/feedback_rules.py 参照
    condition_json    TEXT NOT NULL,
    delivered_count   INTEGER NOT NULL DEFAULT 0,
    overridden_count  INTEGER NOT NULL DEFAULT 0,
    deleted_at        TIMESTAMP,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- strength='block' は実行直前ブロック以外に配達経路を持たないため、
    -- timing='pre_tool' と常に対応させる（片方だけを許すと、知らせる強さなのに
    -- 実行直前タイミングを持つ・止める強さなのに配達経路が無い、という組み合わせが作れてしまう）
    CHECK ((strength = 'block') = (timing = 'pre_tool'))
);

CREATE TABLE feedback_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    INTEGER NOT NULL REFERENCES feedback_entries(id),
    kind        TEXT NOT NULL CHECK (kind IN ('stumble', 'note')),
    body        TEXT NOT NULL CHECK (LENGTH(body) <= 500 AND LENGTH(TRIM(body)) > 0),
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_feedback_notes_entry ON feedback_notes(entry_id);

CREATE TRIGGER trg_feedback_notes_no_update
BEFORE UPDATE ON feedback_notes
BEGIN
    SELECT RAISE(ABORT, 'feedback_notes is append-only; UPDATE not allowed');
END;

CREATE TRIGGER trg_feedback_notes_no_delete
BEFORE DELETE ON feedback_notes
BEGIN
    SELECT RAISE(ABORT, 'feedback_notes is append-only; DELETE not allowed');
END;

-- 保留は session_id × entry_id につき最新1件のみ持つ。別の指紋で再度当たった場合は
-- 新しい指紋でUPSERTし、古い指紋は破棄する（直前の呼び出しと同じ引数でだけ押し切れる）。
CREATE TABLE feedback_holds (
    session_id   TEXT NOT NULL,
    entry_id     INTEGER NOT NULL REFERENCES feedback_entries(id),
    fingerprint  TEXT NOT NULL,  -- sha256(キー順ソートJSON化したtool_input)
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, entry_id)
);

-- prompt_idがhook入力に含まれない場合はNOT NULL DEFAULT ''で受ける。この場合、
-- 同一session_id・entry_idの組はprompt_id=''で常に一致するため、そのエントリは
-- セッションを通じて実質1回しか配達されなくなる（区切りごとに1回、より強い抑制）。
CREATE TABLE feedback_turn_marks (
    session_id  TEXT NOT NULL,
    prompt_id   TEXT NOT NULL DEFAULT '',
    entry_id    INTEGER NOT NULL REFERENCES feedback_entries(id),
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, prompt_id, entry_id)
);

CREATE TABLE feedback_bootstrap_seen (
    session_id  TEXT PRIMARY KEY,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE feedback_switch (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    mode        TEXT NOT NULL DEFAULT 'on' CHECK (mode IN ('off', 'on')),
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO feedback_switch (id, mode) VALUES (1, 'on');
