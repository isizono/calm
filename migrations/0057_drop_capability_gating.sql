-- Migration 0057: capability gating 層の撤去（session_identity テーブル削除 + caller_session_id カラム削除）
--
-- depends: 0056_add_relay_outbox
--
-- destructive: role別capability gating機構（呼び出し元が全て撤去済み）の後始末として
--   session_identity テーブルと caller_session_id カラム群を削除する。データ移行先は無い
--   (機構自体が不要になったための削除で、代替スキーマへの移行ではない)。
--
-- 背景:
--   session_identity / caller_session_id (0048 で追加) は複数 Claude Code セッション
--   (orch / worker / standalone) が並行稼働する構成向けの role-based capability gating
--   機構の一部だった。gating を呼び出していた指揮層（orch/dispatcher/worker体系）が
--   解体されたことで、role 解決自体が機能しなくなり (session_identity への登録経路が
--   撤去済み、env 経路も共有 HTTP デーモンでは per-session に効かない)、gating 機構自体が
--   到達不能な死んだコードになった。本 migration はその機構が使っていたスキーマを撤去する。
--
-- 変更内容:
--   - session_identity テーブルを DROP（紐づく idx_session_identity_role /
--     idx_session_identity_handle / idx_session_identity_ended インデックスはテーブルごと
--     削除されるため個別 DROP 不要）
--   - decisions / discussion_logs / discussion_topics / activities / materials の
--     caller_session_id カラムを DROP
--
-- 対象外（本 migration では触れない）:
--   - search_telemetry / fetch_telemetry の caller_session_id 列（0054 で追加）は
--     search / fetch 呼出の相関キーという別目的で使われており、書込コードは停止するが
--     カラム自体はこの migration の対象外（後方互換のため残置）

DROP TABLE session_identity;

ALTER TABLE decisions          DROP COLUMN caller_session_id;
ALTER TABLE discussion_logs    DROP COLUMN caller_session_id;
ALTER TABLE discussion_topics  DROP COLUMN caller_session_id;
ALTER TABLE activities         DROP COLUMN caller_session_id;
ALTER TABLE materials          DROP COLUMN caller_session_id;
