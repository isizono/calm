-- Migration 0060: 既存importance_scoreデータの補正とCHECK制約追加
--
-- depends: 0059_add_habit_status
--
-- 背景:
--   0058でimportance_scoreを追加した時点では「値が大きいほど優先度が高い」
--   前提のスコアだったが、その後アプリケーション層で1=critical/2=important/
--   3=defaultという順位型の意味づけに変更された。0058のカラム既定値は1.0の
--   ままのため、trigger_mode='intelligently'に切り替え済みだがimportance_score
--   を一度も明示指定していないhabitは、既定値1.0がそのまま新しい意味での
--   1(critical)と解釈され、実際には未トリアージなだけの振る舞いがcriticalラベルで
--   表示されてしまう。
--
--   この意味づけ変更（1=critical/2=important/3=default）を導入するアプリケーション
--   コードと同一のリリースで本migrationを適用するため、適用時点でimportance_score=1.0
--   のまま残っているintelligently habitは「新しい意味でのcriticalとして明示指定された」
--   のではなく「旧既定値が未上書きのまま残っている」ケースであると判別できる
--   （importance_scoreを指定する引数は本migrationとセットのアプリケーション変更で
--   初めて追加されるため、それ以前に1を明示指定する手段自体が存在しない）。
--
-- 変更内容:
--   1. trigger_mode='intelligently'かつimportance_score=1.0（0058の既定値のまま
--      未設定）のhabitをimportance_score=3（default）に補正する
--   2. SQLiteはCHECK制約の後付けができないためテーブルを再構築し、
--      importance_scoreにCHECK(importance_score IN (1, 2, 3))を追加する
--
--   habitsは他エンティティのようにタグ・リレーション・検索インデックスに
--   接続しておらず、参照するトリガー・インデックスも存在しないため、
--   再構築時にそれらの再作成は不要（0026等の再構築migrationと異なる点）。

-- ================================================
-- Step 1: 既存データの補正
-- ================================================

UPDATE habits
SET importance_score = 3
WHERE trigger_mode = 'intelligently' AND importance_score = 1.0;

-- ================================================
-- Step 2: テーブル再構築（importance_scoreにCHECK制約を追加）
-- ================================================

CREATE TABLE habits_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    description TEXT NOT NULL DEFAULT '',
    trigger_mode TEXT NOT NULL DEFAULT 'always' CHECK(trigger_mode IN ('always', 'intelligently')),
    importance_score REAL NOT NULL DEFAULT 1.0 CHECK(importance_score IN (1, 2, 3)),
    last_recalled_at TIMESTAMP NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived'))
);

INSERT INTO habits_new (
    id, content, active, created_at, description, trigger_mode,
    importance_score, last_recalled_at, status
)
SELECT
    id, content, active, created_at, description, trigger_mode,
    importance_score, last_recalled_at, status
FROM habits;

DROP TABLE habits;
ALTER TABLE habits_new RENAME TO habits;
