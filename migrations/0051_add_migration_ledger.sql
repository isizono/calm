-- Migration 0051: migration_ledger テーブル追加
--
-- depends: 0048_session_identity
--
-- 背景:
--   yoyo-migrations の migration_hash は migration_id（拡張子なしファイル名）の sha256 であり、
--   ファイル内容そのものは検証しない。適用済み migration ファイルが worktree 混在や手編集で
--   事後的に書き換わっても yoyo は検知できない。本テーブルは適用時点のファイル内容の
--   sha256 を記録し、起動時に現ファイルと突き合わせて改変を検知するための台帳。
--
-- 設計:
--   - migration_id を PRIMARY KEY とし、1 migration につき最新の内容ハッシュ 1 行を持つ。
--   - applied_at は本台帳への記録時刻（初回導入時のバックフィルでは実際の適用時刻とは
--     一致しない。導入時点の一括登録に限られる近似）。

CREATE TABLE migration_ledger (
    migration_id   TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL,
    applied_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
