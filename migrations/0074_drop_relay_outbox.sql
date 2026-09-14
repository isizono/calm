-- Migration 0074: relay_outbox テーブル削除
--
-- depends: 0073_add_asks_notify_wanted
--
-- destructive: relay専用の送信キュー（transactional outbox）を撤去する。データ移行先は
--   無い（relay統合機能自体の廃止に伴う削除で、代替スキーマへの移行ではない）。
--
-- 背景:
--   relay_outbox（0056で新設）はCALM本体からrelayサーバーへのセッション間通信
--   （publish/subscribe等）を仲介する送信キューだった。CALM本体からrelay統合機能が
--   撤去されたことで、本テーブルへの書き込み・読み出し経路が消滅し、到達不能な
--   データになった。
--
-- 変更内容:
--   - relay_outbox テーブルを DROP（紐づく idx_relay_outbox_pending インデックスは
--     テーブルごと削除されるため個別 DROP 不要）

DROP TABLE relay_outbox;
