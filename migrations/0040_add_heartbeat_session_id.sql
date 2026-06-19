-- Migration 0040: heartbeat に session_id 同梱
--
-- depends: 0039_extend_tag_namespace
--
-- 背景:
--   activities.last_heartbeat_at と組で書き込む last_heartbeat_session_id を導入する。
--   session_start_hook の「## 作業中（別セッション）」セクションが、自セッション自身の
--   heartbeat を「別セッション扱い」と誤表示していた問題を解消するため、書込元の
--   セッションを記録する。
--
-- 設計:
--   - 既存 last_heartbeat_at と同じ activities テーブルに同居（書込が同一トランザクション
--     で完結し、片方だけ更新される状態を生まない）。
--   - nullable: 過去 heartbeat（カラム導入前）や session_id 未知のテストフィクスチャでは
--     NULL のまま。NULL は「未知 → 別セッション扱い」として既存挙動を保つ。

ALTER TABLE activities ADD COLUMN last_heartbeat_session_id TEXT;
