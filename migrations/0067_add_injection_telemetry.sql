-- Migration 0067: injection_telemetry（記録=クエリ添付の追随カウンタ用台帳）新設
--
-- depends: 0062_add_asks
--
-- 背景:
--   記録系ツール（add_logs / add_decisions / add_material）が返す「関連既存記録 top3」
--   （記録=クエリ添付）について、「提示された記録が同セッションで実際に読まれたか」を
--   機械記録するための新規台帳。present（添付を返した瞬間）を1件ずつ記録し、
--   fetch側（既存 fetch_telemetry / search_telemetry）と caller_session_id で
--   post-hoc に JOIN して追随率を算出する。専用の集計ツールは持たない。
--
--   設計正本: docs/design/attachment-follow-through-counter.md（ブランチ
--   docs/deterministic-periphery-designs）。本 migration はそのうち「第3層添付の
--   詳細設計を待たずに先行実装してよい」とされたスキーマ部分のみを対象とする。
--   add_logs / add_decisions / add_material 側から本テーブルへ実際に present 行を
--   書き込む呼出し実装は、添付内容の組み立て方（類似度変換・重複排除等）を規定する
--   第3層添付の詳細設計が別途確定してから追加する（本 migration のスコープ外）。
--
-- スキーマ:
--   caller_session_id : 提示を行ったセッションの相関キー（0048/0054 と同じ ephemeral な
--                        相関キー規約。NULL許容、MCP context 外の呼出はNULL）
--   trigger_tool       : 'add_logs' | 'add_decisions' | 'add_material'
--   source_type/id      : 新規作成された側（添付を提示する記録）の種別とID
--   attached_type/id     : 提示された既存記録の種別とID
--   rank                : 提示順位（1〜3、DB制約なし）
--   similarity          : 0〜1に正規化した類似度（大きいほど類似）。NULL可
--   diagnostics_json     : 将来のretriever内訳等の予備列。NULL可
--
--   FK・UNIQUE制約は張らない（既存 telemetry テーブル群と同じ、生データ台帳としての
--   性質を優先する方針）。同一セッションで同じ(attached_type, attached_id)が複数回
--   提示されるのは正常挙動で、集計側で GROUP BY MIN(timestamp) して縮約する。

CREATE TABLE injection_telemetry (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_session_id  TEXT,
    trigger_tool       TEXT NOT NULL,
    source_type        TEXT NOT NULL,
    source_id          INTEGER NOT NULL,
    attached_type      TEXT NOT NULL,
    attached_id        INTEGER NOT NULL,
    rank               INTEGER NOT NULL,
    similarity         REAL,
    diagnostics_json   TEXT,
    timestamp          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_injection_telemetry_session_ts
    ON injection_telemetry(caller_session_id, timestamp);

CREATE INDEX idx_injection_telemetry_attached
    ON injection_telemetry(attached_type, attached_id);
