-- Migration 0078: activitiesテーブルから orch_managed カラムを削除
--
-- depends: 0077_add_goals
--
-- 背景:
--   orch_managed は複数 Claude Code セッション (orch/worker/standalone) が並行稼働する
--   構成向けに、activity が orch 管理下かどうかを表す属性として追加された (0045)。
--   その運用体系自体が解体され、新規に orch_managed=1 で作成される activity が
--   出なくなった一方、hint抑制・SessionStart一覧除外・Stop hookのnudge抑制など
--   複数箇所で依然として読まれ続けており、「使われていないカラム」と誤読されて
--   実装ミスを誘発していた。読み手の混乱を断つためカラムごと削除する。
--
-- 変更内容:
--   - activities.orch_managed を DROP。PK/UNIQUE/CHECK/FK/INDEX/VIEW/TRIGGER には
--     関与していないため、単純な DROP COLUMN で完結する。
--   - orch_managed=1 で作成されていた既存行は、他のカラムを保持したまま残る。
--     これらは今後、hint・一覧・nudgeの通常の判定対象に戻る。

ALTER TABLE activities DROP COLUMN orch_managed;
