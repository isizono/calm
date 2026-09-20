-- Migration 0075: 自己改善ループの器 — 観測台帳
--
-- depends: 0074_drop_relay_outbox
--
-- 背景:
--   Claudeの振る舞いへの人間の訂正を機械可読な形で溜め、繰り返しの訂正を減らす
--   ための土台（観測台帳）を追加する。本migrationは記録のためのテーブル・参照表・
--   トリガーのみを持ち、配達・採点・書き込みツール・ビューは後続のmigrationで
--   追加する。停止スイッチ（vessel_meta.mode）は既定で観測のみを行う値
--   （'observe'）で始まり、振る舞いに影響しない。
--
-- 変更内容:
--   - 参照表3つ（obs_kinds・lesson_kinds・delivery_channels）と初期行
--   - 観測台帳 obs_events（追記専用）
--   - 知見の見出し lessons・知見スレッドの追記 lesson_entries（いずれも追記専用）
--   - 全文検索用の仮想テーブル lessons_fts（FTS5、trigram）
--   - Stopの読み位置 vessel_cursor・停止スイッチ vessel_meta（いずれも更新可）
--   - 種類と条件の組み合わせを検査するトリガー、常時配達を禁じるトリガー、
--     追記専用を強制するトリガー、lessons_fts を追記に連動させるトリガー

CREATE TABLE obs_kinds (kind TEXT PRIMARY KEY);
INSERT INTO obs_kinds VALUES ('utterance'),('speaker'),('reply'),('tool'),('tool_overflow'),
  ('tool_fail'),('delivered'),('suppressed'),('stepped'),('bind'),('boundary'),('human_withdraw');

CREATE TABLE lesson_kinds (kind TEXT PRIMARY KEY,
  delivers INTEGER NOT NULL CHECK (delivers IN (0,1)),
  steps    INTEGER NOT NULL CHECK (steps IN (0,1) AND steps <= delivers));
-- prevent: 配達条件・踏み跡条件を両方持つ。tally: 条件を持たず配達しない。
-- guide: 配達条件だけを持つ。書き込みツールが新規作成を拒否するため、初期値の投入
-- でだけ使われる（運用中にClaudeが作れるのは delivers=1 AND steps=1 の組だけ）。
INSERT INTO lesson_kinds VALUES ('prevent',1,1),('tally',0,0),('guide',1,0);

CREATE TABLE delivery_channels (channel TEXT PRIMARY KEY);
INSERT INTO delivery_channels VALUES ('session'),('prompt'),('post_tool'),('tool_fail'),('pull');

CREATE TABLE obs_events (          -- 観測台帳。器のhookだけが書く。追記専用。前後の判定は id で行う
  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
  prompt_id TEXT, agent_id TEXT,   -- prompt_id は最初の入力より前は無い。agent_id は印字モード(headless)
                                    -- 起動のサブエージェント内でだけ入る。対話セッション下のサブエージェント
                                    -- では入らない
  kind TEXT NOT NULL REFERENCES obs_kinds(kind),
  flag TEXT CHECK (flag IN ('strong','weak')),   -- 発話の地の文が語彙に当たったか
  text TEXT, tool_name TEXT, tool_use_id TEXT,
  lesson_id INTEGER REFERENCES lessons(id), entry_id INTEGER REFERENCES lesson_entries(id),
  ref_id INTEGER REFERENCES obs_events(id),      -- speaker・bind・human_withdraw が指す行
  channel TEXT REFERENCES delivery_channels(channel),
  src_uuid TEXT,                                 -- reply の元の transcript のメッセージ識別子
  created_at TEXT NOT NULL DEFAULT (datetime('now')),   -- 記録用。判定には使わない
  CHECK (kind NOT IN ('delivered','suppressed','stepped','bind','human_withdraw') OR lesson_id IS NOT NULL),
  CHECK (kind <> 'human_withdraw' OR ref_id IS NOT NULL),
  CHECK (kind NOT IN ('delivered','suppressed') OR channel IS NOT NULL), CHECK (kind = 'utterance' OR flag IS NULL),
  CHECK (kind NOT IN ('utterance','reply','speaker') OR text IS NOT NULL), CHECK (kind <> 'speaker' OR ref_id IS NOT NULL),
  CHECK (kind NOT IN ('tool','tool_fail') OR tool_name IS NOT NULL), UNIQUE (session_id, src_uuid));
CREATE UNIQUE INDEX uq_obs_speaker ON obs_events(ref_id) WHERE kind = 'speaker';  -- 発話ごとに1行
CREATE INDEX idx_obs_session ON obs_events(session_id, kind, prompt_id);
CREATE INDEX idx_obs_lesson  ON obs_events(lesson_id, kind, session_id);

CREATE TABLE lessons (             -- 知見の見出し。作成後は不変
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL REFERENCES lesson_kinds(kind),
  handle TEXT NOT NULL UNIQUE CHECK (handle NOT GLOB '*[^a-z0-9-]*' AND length(handle) BETWEEN 3 AND 40),
  body TEXT NOT NULL CHECK (length(body) <= 300),
  deliver_event TEXT CHECK (deliver_event IN ('session','prompt','tool_call','tool_fail')),
  deliver_spec TEXT,               -- 条件JSON。書き込みツールがキー順と空白を正規化して入れる
  step_event TEXT CHECK (step_event IN ('tool_call','tool_fail','reply')), step_spec TEXT,
  quote TEXT CHECK (quote IS NULL OR length(quote) BETWEEN 8 AND 300),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK ((deliver_event IS NULL) = (deliver_spec IS NULL)), CHECK ((step_event IS NULL) = (step_spec IS NULL)));

CREATE TABLE lesson_entries (      -- 知見スレッドの追記。追記専用
  id INTEGER PRIMARY KEY, lesson_id INTEGER NOT NULL REFERENCES lessons(id),
  kind TEXT NOT NULL CHECK (kind IN ('body','conditions','note','violated','contradicted','withdraw')),
  body TEXT CHECK (body IS NULL OR length(body) <= 300),
  note TEXT CHECK (note IS NULL OR length(note) <= 150),
  deliver_event TEXT, deliver_spec TEXT, step_event TEXT, step_spec TEXT,  -- lessons と同じ値のCHECK
  quote TEXT CHECK (quote IS NULL OR length(quote) BETWEEN 8 AND 300),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK ((kind = 'body') = (body IS NOT NULL)), CHECK ((kind = 'note') = (note IS NOT NULL)),
  CHECK (kind = 'conditions' OR coalesce(deliver_event, deliver_spec, step_event, step_spec) IS NULL),
  CHECK (kind NOT IN ('violated','contradicted') OR quote IS NOT NULL));

CREATE VIRTUAL TABLE lessons_fts USING fts5(handle, body, quote, tokenize = 'trigram');

CREATE TABLE vessel_cursor (session_id TEXT PRIMARY KEY, byte_offset INTEGER NOT NULL);  -- 更新を許す
CREATE TABLE vessel_meta (id INTEGER PRIMARY KEY CHECK (id = 1),
  mode TEXT NOT NULL DEFAULT 'observe' CHECK (mode IN ('off','observe','on')));      -- 更新を許す
INSERT INTO vessel_meta (id) VALUES (1);

-- lessons: 種類表に条件の有無を合わせ、踏み跡を持たない種類への常時配達を拒否する
CREATE TRIGGER lessons_kind_shape BEFORE INSERT ON lessons BEGIN
  SELECT RAISE(ABORT, 'vessel:kind_mismatch') WHERE NOT EXISTS (SELECT 1 FROM lesson_kinds k
    WHERE k.kind = NEW.kind AND k.delivers = (NEW.deliver_event IS NOT NULL)
      AND k.steps = (NEW.step_event IS NOT NULL));
  SELECT RAISE(ABORT, 'vessel:no_session_channel')
    WHERE NEW.deliver_event = 'session' AND EXISTS (SELECT 1 FROM lesson_kinds k
      WHERE k.kind = NEW.kind AND k.delivers = 1 AND k.steps = 0);
END;

-- lesson_entries の conditions 追記: 対象の知見の種類が配達しない種類なら拒否し、
-- 配達する種類なら lessons_kind_shape と同じ形で条件の有無・常時配達を検査する
CREATE TRIGGER lesson_entries_conditions_shape BEFORE INSERT ON lesson_entries
WHEN NEW.kind = 'conditions'
BEGIN
  SELECT RAISE(ABORT, 'vessel:kind_mismatch') WHERE NOT EXISTS (
    SELECT 1 FROM lessons l JOIN lesson_kinds k ON k.kind = l.kind
    WHERE l.id = NEW.lesson_id AND k.delivers = 1
      AND k.delivers = (NEW.deliver_event IS NOT NULL)
      AND k.steps = (NEW.step_event IS NOT NULL));
  SELECT RAISE(ABORT, 'vessel:no_session_channel') WHERE NEW.deliver_event = 'session' AND EXISTS (
    SELECT 1 FROM lessons l JOIN lesson_kinds k ON k.kind = l.kind
    WHERE l.id = NEW.lesson_id AND k.delivers = 1 AND k.steps = 0);
END;

-- 追記専用の強制: obs_events・lessons・lesson_entries・lesson_kinds は
-- INSERTのみを許し、UPDATE・DELETEを拒否する（種類表は候補が行を足すだけで、
-- 既存の行の意味を変えない）
CREATE TRIGGER trg_obs_events_append_only_upd BEFORE UPDATE ON obs_events
BEGIN SELECT RAISE(ABORT, 'vessel:append_only'); END;
CREATE TRIGGER trg_obs_events_append_only_del BEFORE DELETE ON obs_events
BEGIN SELECT RAISE(ABORT, 'vessel:append_only'); END;

CREATE TRIGGER trg_lessons_append_only_upd BEFORE UPDATE ON lessons
BEGIN SELECT RAISE(ABORT, 'vessel:append_only'); END;
CREATE TRIGGER trg_lessons_append_only_del BEFORE DELETE ON lessons
BEGIN SELECT RAISE(ABORT, 'vessel:append_only'); END;

CREATE TRIGGER trg_lesson_entries_append_only_upd BEFORE UPDATE ON lesson_entries
BEGIN SELECT RAISE(ABORT, 'vessel:append_only'); END;
CREATE TRIGGER trg_lesson_entries_append_only_del BEFORE DELETE ON lesson_entries
BEGIN SELECT RAISE(ABORT, 'vessel:append_only'); END;

CREATE TRIGGER trg_lesson_kinds_append_only_upd BEFORE UPDATE ON lesson_kinds
BEGIN SELECT RAISE(ABORT, 'vessel:append_only'); END;
CREATE TRIGGER trg_lesson_kinds_append_only_del BEFORE DELETE ON lesson_kinds
BEGIN SELECT RAISE(ABORT, 'vessel:append_only'); END;

-- lessons_fts の追記連動: lessons のINSERTでFTS行を作り、body追記で本文列だけ更新する
CREATE TRIGGER trg_lessons_fts_insert AFTER INSERT ON lessons BEGIN
  INSERT INTO lessons_fts (rowid, handle, body, quote) VALUES (NEW.id, NEW.handle, NEW.body, NEW.quote);
END;
CREATE TRIGGER trg_lesson_entries_fts_body AFTER INSERT ON lesson_entries
WHEN NEW.kind = 'body'
BEGIN
  UPDATE lessons_fts SET body = NEW.body WHERE rowid = NEW.lesson_id;
END;
