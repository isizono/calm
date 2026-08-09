<!-- ccm-doc-sync
watch-tags: domain:cc-memory
watch-direction: true
watch-migrations: true
last-synced: 2026-08-02
last-synced-migration: 0068
-->

# cc-memory DBスキーマ v0

## 0. 読み方

本ドキュメントは cc-memory 本体（SQLite データベース）のスキーマについて、設計判断の背景・変遷・既知の課題をまとめた手書きの作業用ドキュメントである。凍結を目的としない。

**カラム名・型・NULL可否・デフォルト値・インデックスの現在値は本ドキュメントには書かない。** これらは `docs/spec/db-schema-tables.md`（`scripts/dump_db_schema.py` が `migrations/` 全適用後の実スキーマから自動生成、手動編集禁止）を正とする。本ドキュメントは「今どういう形か」ではなく「なぜこの形なのか」に集中する。§3 各テーブル節は用途・変遷（旧カラムの追加/削除履歴等）・設計判断の背景のみを記述し、正確なカラム一覧は都度 `db-schema-tables.md` の該当節へのリンクで参照する。

- 一次情報は `migrations/` 配下の yoyo マイグレーションファイル全体である
- 表面化していない（コード読み込みでも確認できない）事項は「未確認」と明記する
- 既知の課題は5次元統合レポートの T1（エンティティモデル & データアーキ）の指摘事実を §8 にまとめる。解消済みの項目は削除せず、取り消し線 + 解消日で残す（詳細は `docs/spec/doc-sync-convention.md` §6）

凡例:
- カラムごとの「関連 migration」は、最後にそのカラム形状を確定したファイル番号を示す
- 論理的なエンティティ名（topic / activity / decision / log / material / habit）と物理テーブル名（`discussion_topics` / `activities` / `decisions` / `discussion_logs` / `materials` / `habits`）の両方を併記する

---

## 1. 全体像

```mermaid
erDiagram
  discussion_topics ||--o{ topic_tags : ""
  activities ||--o{ activity_tags : ""
  decisions ||--o{ decision_tags : ""
  discussion_logs ||--o{ log_tags : ""
  materials ||--o{ material_tags : ""
  asks ||--o{ ask_tags : ""
  tags ||--o{ topic_tags : ""
  tags ||--o{ activity_tags : ""
  tags ||--o{ decision_tags : ""
  tags ||--o{ log_tags : ""
  tags ||--o{ material_tags : ""
  tags ||--o{ ask_tags : ""
  tags ||--o| tags : "canonical_id"

  activities ||--o{ activity_dependencies : "depends_on"
  decisions ||--o{ decision_supersedes : "supersedes"
  decisions ||--o{ decision_destabilization_resolutions : "resolves"

  discussion_topics ||--o{ relations : "polymorphic"
  activities ||--o{ relations : "polymorphic"
  decisions ||--o{ relations : "polymorphic"
  discussion_logs ||--o{ relations : "polymorphic"
  materials ||--o{ relations : "polymorphic"

  discussion_topics ||--o{ pins : "polymorphic"
  activities ||--o{ pins : "polymorphic"
  decisions ||--o{ pins : "polymorphic"
  discussion_logs ||--o{ pins : "polymorphic"
  materials ||--o{ pins : "polymorphic"
  tags ||--o{ pins : "polymorphic"

  discussion_topics ||--|| search_index : "trigger sync"
  activities ||--|| search_index : "trigger sync"
  decisions ||--|| search_index : "trigger sync"
  discussion_logs ||--|| search_index : "trigger sync"
  materials ||--|| search_index : "trigger sync"
  search_index ||--|| search_index_fts : "rowid"
  search_index ||--|| vec_index : "rowid (app layer)"
  tags ||--|| tag_vec : "rowid (app layer)"

  habits {
    int id
    text content
    int active
    text description
    text trigger_mode
    real importance_score
    timestamp last_recalled_at
    text status
  }
```

補足:
- `relations` / `pins` はポリモーフィックな多対多関係を1テーブルで束ねる。図上はエンティティ別の線で表現したが、物理的には `(source_type, source_id, target_type, target_id)` の文字列 + 整数の PK 組み合わせで識別する
- decision / log の親 topic 帰属も 0046 以降は `relations`（`relation_type='belongs_to'`）で表現する。旧 `decisions.topic_id` / `discussion_logs.topic_id` の直接 FK は 0047 で物理削除済みのため、図上に個別の辺としては描いていない（§3.3 / §3.4 / §4 参照）
- `tag_vec` / `vec_index` は sqlite-vec の仮想テーブルで、外部 FK を張れないためアプリ層で同期する
- `habits` は他エンティティ群とは独立しており、タグ・リレーション・検索インデックスのいずれにも接続していない
- 図には含めていないが、`search_telemetry`（0041）/ `citations`（0042）/ `citation_event_log`（0046）の3テーブルが 0040 以降に追加されている。いずれも既存5エンティティのライフサイクル（タグ・relations・search_index）には接続しない補助テーブルのため省略した。詳細は §2 / §3 を参照
- `session_identity` テーブルと5エンティティの `caller_session_id` カラムは 0048 で追加されたが、role-based capability gating 機構（呼び出し元の指揮層解体に伴い不要化）の撤去とあわせて 0057 で削除された

---

## 2. テーブル一覧

| 物理テーブル名 | 論理名 | 主用途 |
|---|---|---|
| `discussion_topics` | topic | 議論の器（関心事・問題・機能） |
| `activities` | activity | 作業の器（intent + status を持つ作業単位） |
| `decisions` | decision | 双方が合意した決定事項 |
| `discussion_logs` | log | 議論や作業の経緯を残す記録 |
| `materials` | material | 生成された成果物（ドラフト・分析・調査レポートなど） |
| `habits` | habit | 全セッション共通の行動ルール |
| `tags` | tag | namespace + name による分類タグ |
| `topic_tags` | — | topic ↔ tag junction |
| `activity_tags` | — | activity ↔ tag junction |
| `decision_tags` | — | decision ↔ tag junction |
| `log_tags` | — | log ↔ tag junction |
| `material_tags` | — | material ↔ tag junction |
| `relations` | — | 5エンティティ間の related 関係 + topic への belongs_to 帰属（ポリモーフィック） |
| `activity_dependencies` | — | activity 間の有向 depends_on |
| `decision_supersedes` | — | decision 間の有向 supersedes/destabilizes（kind列で区別） |
| `decision_destabilization_resolutions` | — | destabilizesエッジ単位の解消記録（reaffirmed/revised/retracted） |
| `pins` | — | 任意エンティティ間の有向 pin（注意フラグ） |
| `search_index` | — | 全エンティティ統一の検索インデックス中間テーブル |
| `search_index_fts` | — | search_index と rowid 連動する contentless FTS5 仮想テーブル |
| `vec_index` | — | search_index と rowid 連動する sqlite-vec 仮想テーブル（384次元） |
| `tag_vec` | — | tags と rowid 連動する sqlite-vec 仮想テーブル（384次元） |
| `relations_view` | — | relations / activity_dependencies / decision_supersedes を統合した VIEW |
| `search_telemetry` | — | search() 呼出ごとのquery/parameters/結果件数の記録（運用計測用） |
| `citations` | — | 本文中の `{{cite:X#NNN}}` 参照の構造化保存 |
| `citation_event_log` | — | write時sanitize等のテキスト変換イベントの逐次記録（旧 sanitize_log の後継） |
| `signal_events` | signal | cc-memory自身の故障・使用感不満・矛盾検出・運用計測イベントの記録先 |
| `asks` | ask | 人間の判断を待つ問いの記録先（判断委譲の受け皿） |
| `ask_blocks` | — | ask ↔ activity junction（このaskが答え待ちで止めているactivity） |
| `ask_requesters` | — | ask ↔ 要求元session_id junction（UNION蓄積） |
| `ask_tags` | — | ask ↔ tag junction |
| `ask_vec` | — | asks と rowid 連動する sqlite-vec 仮想テーブル（384次元、cosine距離） |
| `injection_telemetry` | — | 記録=クエリ添付（記録系ツールの関連既存記録top3提示）の追随カウンタ present側台帳 |

行数感（規模）はランタイム情報のため本ドキュメントでは未記載とする。

---

## 3. 各テーブル詳細

カラム名・型・NULL可否・デフォルト値・インデックスの現在値は `docs/spec/db-schema-tables.md`（自動生成）を参照。以下は各テーブルの用途・変遷・設計判断の背景に絞った手書きメモである。

### 3.1 discussion_topics

議論トピック。1つの関心事・問題・機能を表す。

補足:
- 0001 で project_id / parent_topic_id を持つ階層構造として作成されたが、0010 で両カラムとも削除された
- 親子帰属は現状もたず、トピック間関連は `relations` テーブル（`source_type='topic'`）で表現する
- caller_session_id カラムは 0048 で追加されたが、0057 で削除された（§6）

関連 migration: 0001（新設）/ 0003（project→subject リネーム）/ 0010（subject_id, parent_topic_id 削除）/ 0048（caller_session_id 追加、のち0057で削除）

カラム一覧・インデックス: `db-schema-tables.md` の `discussion_topics` 節参照。

### 3.2 activities

作業の器。status を持つ。

補足:
- 0001 で tasks として作成され、0011 で activities にリネーム
- 0007 で `blocked` status 削除、0026 で `snoozed` 追加、0027 で `shelved` 追加
- topic_id は 0001 で存在 → 0010 で削除 → 0016 で復活 → 0021 で relations 化に伴い再削除、という往復履歴を持つ。現状は relations テーブル経由でトピックに紐づける
- last_heartbeat_session_id は 0040 で追加。自セッションのheartbeatを「別セッション扱い」と誤表示していた問題の解消用
- orch_managed は 0045 で追加。従来の素タグ `orch-managed` の存在/不在で表現していた属性を構造的カラムへ昇格したもの（同migrationで既存タグ付きactivityへの一括反映も実施）
- caller_session_id カラムは 0048 で追加されたが、0057 で削除された（§6）

関連 migration: 0001 / 0007 / 0010 / 0011 / 0016 / 0017 / 0021 / 0026 / 0027 / 0040（last_heartbeat_session_id）/ 0045（orch_managed）/ 0048（caller_session_id追加、のち0057で削除）

カラム一覧・インデックス: `db-schema-tables.md` の `activities` 節参照。

### 3.3 decisions

双方が合意した決定事項。

補足:
- 0001 では topic_id が NULL 許容だったが、0005（重複番号片方）で NOT NULL 化された（`first_topic` への移行付き）。0006 で ON DELETE CASCADE 追加
- 0031 で retracted_at 導入、0037 で title 追加
- pinned カラムは 0029 で追加されたが、0034/0035 で pins テーブル化により削除された
- **`topic_id` カラムは現在存在しない**。0046 で NULLABLE 化 + FK 制約撤去、0047 で物理削除された。親 topic 帰属は `relations`（`source_type='decision', target_type='topic', relation_type='belongs_to'`）で表現する（§3.9 / §4 参照）。旧 `idx_decisions_topic_id` インデックスも 0046 で撤去済み
- caller_session_id カラムは 0048 で追加されたが、0057 で削除された（§6）

関連 migration: 0001 / 0005（topic_id NOT NULL、のち撤去）/ 0006 / 0029 / 0031 / 0035 / 0037 / 0046（topic_id NULLABLE化・belongs_to複製）/ 0047（topic_id 物理削除）/ 0048（caller_session_id追加、のち0057で削除）

カラム一覧・インデックス: `db-schema-tables.md` の `decisions` 節参照。

### 3.4 discussion_logs

議論や作業の経緯。

補足:
- 0001 では content のみ。0008 で title カラム追加 + 検索インデックス登録
- 0006 で ON DELETE CASCADE 追加
- 0031 で retracted_at 導入
- pinned カラムは 0029 で追加 → 0035 で削除
- **`topic_id` カラムは現在存在しない**（decisions と同じ経緯。0046 で NULLABLE化、0047 で物理削除）。親 topic 帰属は `relations`（`relation_type='belongs_to'`）で表現する。旧 `idx_logs_topic_id` インデックスも 0046 で撤去済み
- caller_session_id カラムは 0048 で追加されたが、0057 で削除された（§6）

関連 migration: 0001 / 0006 / 0008 / 0029 / 0031 / 0035 / 0046（topic_id NULLABLE化・belongs_to複製）/ 0047（topic_id 物理削除）/ 0048（caller_session_id追加、のち0057で削除）

カラム一覧・インデックス: `db-schema-tables.md` の `discussion_logs` 節参照。

### 3.5 materials

成果物（ドラフト・分析結果・調査レポート等）。

補足:
- 0013 で activity_id FK 直結エンティティとして新設 → 0023 で activity_id 削除し独立エンティティ化、relations 経由で activity に紐づく構成へ
- 0029 で pinned カラム追加 → 0034 で pins テーブルへ移行 → 0035 で pinned カラム削除
- 0032 で source カラム追加、0036 で updated_at カラム追加
- retracted_at は 0043 で追加された（decision/log の retract 機構（0031）に対称化）。§6 の記載（「decision/log にのみあり material には無い」）は 0043 時点で古くなっている
- caller_session_id カラムは 0048 で追加されたが、0057 で削除された（§6）

関連 migration: 0013 / 0018 / 0023 / 0029 / 0032 / 0034 / 0035 / 0036 / 0043（retracted_at）/ 0048（caller_session_id追加、のち0057で削除）

カラム一覧・インデックス: `db-schema-tables.md` の `materials` 節参照。

### 3.6 habits

全セッション共通の行動ルール（旧 reminders）。

補足:
- 0019 で reminders として新設、0025 で habits にリネーム
- タグ・リレーション・検索インデックス・embedding のいずれにも接続しない。事実上「設定/ポリシー」として独立
- 0058 で description / trigger_mode / importance_score / last_recalled_at を追加し、SessionStart全件全文注入からalways/intelligently分割注入に変更
- 0059 で status を追加し、intelligently層マニフェストのソートに importance_score（昇順、1が最優先）を使うよう変更
- 0060 で、0058時点の既定値1.0が新しい意味づけ（1=critical）と衝突しないよう
  trigger_mode='intelligently'かつimportance_score=1.0（未設定）のhabitを3（default）に
  補正したうえで、importance_scoreにCHECK(IN (1, 2, 3))を追加（テーブル再構築）
- last_recalled_at はレンダー時decay述語（`is_decay_eligible`）の入力でもあり、trigger_mode='intelligently'の
  habitで作成から `HABIT_MANIFEST_DECAY_DAYS`（既定90日）を超え、かつこの値も同日数以内に更新されていない
  場合はマニフェスト表示から除外される
- 0065 で、`trigger_mode='always'` かつ `active=1` な habit の content 合計文字数が
  2000字を超えて増加する INSERT/UPDATE を `RAISE(ABORT)` で拒否するDBトリガー
  （ラチェット型天井、縮む変更は天井超過中でも常に許可）を追加。アプリ層
  （habit_service）が持つ1500字ゲートとは独立した、直接書き込み経路向けの保険

関連 migration: 0019 / 0022（初期データ追加）/ 0025 / 0058（description / trigger_mode / importance_score / last_recalled_at 追加）/ 0059（status 追加）/ 0060（importance_score データ補正 + CHECK制約追加）/ 0065（always プール合計ラチェット天井トリガー追加）

カラム一覧・インデックス: `db-schema-tables.md` の `habits` 節参照。

### 3.7 tags

namespace + name による分類タグ。

補足:
- 0009 で新設。当初 namespace CHECK は `('', 'domain', 'scope', 'mode')`
- 0014 で `scope` を素タグに降格、`mode` → `intent` リネーム、CHECK = `('', 'domain', 'intent')`
- 0012 で notes、0015（重複番号片方）で canonical_id、0024 で description 追加
- 0039（重複番号片方 `extend_tag_namespace`）で namespace CHECK 制約自体を撤廃し、任意 TEXT を受け付ける形に再構築（妥当性は Python 層で検証）
- 0061 で archived_at / archived_reason 追加。tag notes の自動注入からは除外しつつ、
  search 等の取得系では削除せずラベル付きで下位表示するための退役フラグ
- 0064 で last_injected_at 追加。tag notes の遭遇時注入（`collect_tag_notes_for_injection`）が
  実際に notes を全文配信した実績を記録し、レンダー時decay述語（`is_decay_eligible`）の入力に使う。
  作成から `TAG_NOTES_DECAY_DAYS`（既定180日）を超え、かつこの値も同日数を超えて更新されていない
  タグは自動注入時に notes 全文の代わりに1行ポインタ文言へ縮退する
- 0066 で、notes が4000字を超えて増加する INSERT/UPDATE を `RAISE(ABORT)` で拒否する
  DBトリガー（1タグあたりのラチェット型天井、縮む変更は天井超過中でも常に許可）を追加

関連 migration: 0009 / 0012 / 0014 / 0015_tag_canonical / 0024 / 0039_extend_tag_namespace / 0061_add_tag_archived / 0064_add_tags_last_injected_at / 0066_add_tags_notes_ratchet_trigger

カラム一覧・インデックス: `db-schema-tables.md` の `tags` 節参照。

### 3.8 topic_tags / activity_tags / decision_tags / log_tags / material_tags

各エンティティ ↔ tag の junction table。構造は全て同型。

補足:
- `task_tags` として 0009 で作成 → 0011 で `activity_tags` にリネーム（`task_id` → `activity_id`）
- `material_tags` は 0023 で追加

関連 migration: 0009 / 0011 / 0023 / 0049

カラム一覧・インデックス: `db-schema-tables.md` の `topic_tags` 節参照。

### 3.9 relations

5エンティティ（topic / activity / decision / log / material）間の対称な「related」関係を1テーブルに統合したポリモーフィック関係表。

正規化制約:
- `CHECK (source_type < target_type OR (source_type = target_type AND source_id < target_id))`
- これにより重複・逆順格納が物理的に排除される

補足:
- 0033 で5つの個別 relation テーブル（topic_relations / topic_activity_relations / activity_relations / topic_material_relations / activity_material_relations）を統合し新設
- ポリモーフィック FK のため DB 側 FK 制約は張れず、CASCADE は trigger（`trg_relations_cascade_delete_*` 5本）で実現
- 0033 時点では `relation_type` は CHECK で `'related'` 固定のデッドカラムだった（depends_on / supersedes は別テーブル）
- **0046 で `relation_type` の CHECK を `('related', 'belongs_to')` に緩和**。子→親（decision/log/material/activity → topic）の帰属を表す `belongs_to` を追加し、全5エンティティの親帰属をこの1系統に統一した（旧 `decisions.topic_id` / `discussion_logs.topic_id` FK・旧 material/activity の `related` 流用を置き換え）
- `belongs_to` クエリの hot path 用に partial index を2本追加（0046）: `idx_relations_belongs_to_tgt`（子→親方向）/ `idx_relations_belongs_to_src`（親→子方向）

関連 migration: 0020（旧個別テーブル新設）/ 0023（material 系追加）/ 0033（統合）/ 0046（belongs_to 追加・partial index 2本）

カラム一覧・インデックス: `db-schema-tables.md` の `relations` 節参照。

### 3.10 activity_dependencies

activity 間の有向 `depends_on` 関係。

制約:
- `CHECK (dependent_id != dependency_id)`
- 循環検出は relation_service（DFS）で実装される

関連 migration: 0028

カラム一覧・インデックス: `db-schema-tables.md` の `activity_dependencies` 節参照。

### 3.11 decision_supersedes

decision 間の有向関係。`kind`列で意味論を2つに分ける。`replaces`（結論の置き換え、旧`supersedes`と同義）と`destabilizes`（前提が変わったので要再検証、結論が変わるとは限らない）。

制約:
- `CHECK (source_id != target_id)`
- 同一`(source_id, target_id)`ペアに`replaces`/`destabilizes`両方が共存することはスキーマ上許容される（PKに`kind`を含むため）

関連 migration: 0033（新設）/ 0063（kind列追加、PK再構成）

カラム一覧・インデックス: `db-schema-tables.md` の `decision_supersedes` 節参照。

### 3.11a decision_destabilization_resolutions

`decision_supersedes`の`kind='destabilizes'`エッジ1本ごとの解消記録。エッジ自体は解消後も`decision_supersedes`から削除しない（履歴保存）。

関連 migration: 0063

カラム一覧・インデックス: `db-schema-tables.md` の `decision_destabilization_resolutions` 節参照。

### 3.12 pins

任意エンティティから任意エンティティへの有向 pin（注意フラグ）。

補足:
- 0034 で新設、従来の `discussion_logs.pinned` / `decisions.pinned` / `materials.pinned` カラム機構を置き換える
- 既存 `materials.pinned=1` 行は 0034 で `(source='activity', target='material')` の形で移行
- ポリモーフィック FK のため CASCADE は trigger（`trg_pins_cascade_delete_*` 6本：topic / activity / material / decision / log / tag）で実現
- 0035 で旧 pinned カラムをすべて DROP

関連 migration: 0034 / 0035 / 0038 / 0039_extend_tag_namespace（tag trigger 再作成）

カラム一覧・インデックス: `db-schema-tables.md` の `pins` 節参照。

### 3.13 search_index

5エンティティ（topic / activity / decision / log / material）の検索メタ情報を統一格納する中間テーブル。本文は持たず、対応する FTS5 / vec_index は rowid（= `search_index.id`）で連動する。

補足:
- 0002 で新設（当初は body を持たず、search_index_fts と分離）
- 0003 で `project_id`、0009 で `subject_id` を保持していたが 0010 で削除
- 0030 で created_at カラム追加（5エンティティ分バックフィル）
- 各エンティティ側に INSERT/UPDATE/DELETE トリガーがあり、本テーブルと FTS5 仮想テーブルへ同時に書き込まれる
- vec_index は trigger 連動ではなく、アプリ層（embedding_service）が rowid 整合性を保つ

関連 migration: 0002 / 0003 / 0008 / 0010 / 0018 / 0030 / 0037 / 0049

カラム一覧・インデックス: `db-schema-tables.md` の `search_index` 節参照。

### 3.14 search_index_fts

contentless FTS5 仮想テーブル。`search_index.id` を rowid に持ち、`title` と `body` を全文検索する。

| カラム | 説明 |
|---|---|
| title | 全文検索用 title |
| body | 全文検索用 body |

設定:
- `content=''`（contentless）
- `tokenize='trigram'`

補足:
- contentless のため `snippet()` は本文を保持しない（先頭 N 字を返すのみ、FTS マッチ位置から抽出されない）
- 全エンティティのトリガー（`trg_search_*_insert/update/delete`）が search_index と並行に FTS5 を更新する

関連 migration: 0002（新設）/ 0003 / 0004 / 0005 / 0006 / 0007 / 0008 / 0010 / 0011 / 0018 / 0026 / 0027 / 0030 / 0037（全エンティティのトリガー再生成回）

カラム一覧・インデックス: `db-schema-tables.md` の `search_index_fts` 節参照。

### 3.15 vec_index

sqlite-vec の vec0 仮想テーブル（384次元）。`search_index.id` を rowid に対応させてベクトル検索を行う。

| カラム | 説明 |
|---|---|
| embedding | float[384] |

補足:
- 仮想テーブルのため FK 制約不可。孤児削除はアプリ層（embedding_service）の責務
- 384次元はモデル依存（未確認: 利用モデル名はコード側に固定値）

関連 migration: 0005_add_vec_index

カラム一覧・インデックス: `db-schema-tables.md` の `vec_index` 節参照。

### 3.16 tag_vec

tags テーブル用の sqlite-vec 仮想テーブル（384次元）。tag embedding によるタグ KNN マージ判定で使われる。

| カラム | 説明 |
|---|---|
| embedding | float[384] |

補足:
- 仮想テーブルのため FK 制約不可。`tags.id` を rowid として運用するが整合性はアプリ層任せ

関連 migration: 0009

カラム一覧・インデックス: `db-schema-tables.md` の `tag_vec` 節参照。

### 3.17 relations_view

`relations` / `activity_dependencies` / `decision_supersedes` を統合した読み取り専用 VIEW。

カラム:
| カラム | 説明 |
|---|---|
| source_type | 起点種別 |
| source_id | 起点ID |
| target_type | 終点種別 |
| target_id | 終点ID |
| relation_type | `'related'` / `'belongs_to'` / `'depends_on'` / `'supersedes'` / `'destabilizes'` のいずれか |
| created_at | 作成時刻 |

構成:
- `related` / `belongs_to`: `relations` テーブルを正方向 + 逆方向の UNION ALL で展開し、`relation_type` カラムをそのまま返す（0046 以前は `'related'` リテラル固定で返していたが、`belongs_to` 追加に伴い直接返す形に再構築された）
- `depends_on`: `activity_dependencies` をそのまま（非対称）
- `supersedes` / `destabilizes`: `decision_supersedes` の`kind`列を`CASE`で`relation_type`に出し分け（非対称）

関連 migration: 0020（初版）/ 0023（material 系拡張）/ 0028（depends_on 追加）/ 0033（relations 統合 + supersedes 追加）/ 0046（belongs_to 対応・relation_type を直接返す形に再構築）/ 0063（destabilizes 出し分け追加）

カラム一覧・インデックス: `db-schema-tables.md` の `relations_view` 節参照。

### 3.18 search_telemetry

search() 呼出ごとのquery/parameters/結果件数を記録する運用計測テーブル。書込は別スレッドで非同期に行い、失敗時は`logger.warning`のみでsearch本体には影響させない。

関連 migration: 0041（新設）

カラム一覧・インデックス: `db-schema-tables.md` の `search_telemetry` 節参照。

### 3.19 citations

本文中の `{{cite:X#NNN}}`（X = M/D/L/A/T、material/decision/log/activity/topic に対応）テンプレ参照を構造化保存するテーブル。

補足:
- owner削除時はtriggerでcascade削除（`trg_citations_cascade_delete_*` 5本）
- target側のretract/物理削除時はcitations行を残置する（監査トレース要件）。展開時にdangling判定を動的に行う

関連 migration: 0042（新設）

カラム一覧・インデックス: `db-schema-tables.md` の `citations` 節参照。

### 3.20 citation_event_log

テキスト変換イベント（write時sanitize / bulk migration / transcript hookでのsanitize等）を1イベント1行で逐次記録するテーブル。0044で作られた集計カウンタ型`sanitize_log`（INSERT経路未実装のまま据え置き）を0046で作り直したもの。

source ENUM値: `write_auto_convert` / `bulk_migration` / `transcript_post_tool_use` / `transcript_session_start_backfill` / `external_doc_sanitize`

VIEW 3本（同migrationで新設）:
- `sanitize_event_log`: transcript系 + external_doc_sanitize由来（純粋なサニタイズ系）
- `auto_convert_event_log`: write_auto_convert / bulk_migration由来（自動変換系）
- `citation_event_log_by_entity`: target単位の集約（event_count, last_occurred_at）

関連 migration: 0044（sanitize_log として新設、forward-onlyでDROP済み）/ 0046（citation_event_logとして作り直し）

カラム一覧・インデックス: `db-schema-tables.md` の `citation_event_log` 節参照。

### 3.21 signal_events

cc-memory自身の故障報告・使用感不満・矛盾検出・運用計測イベントの統一記録先。decision / log とは異なり「双方の合意」も文脈タグ体系も要らない生の観測データであり、量が多く状態遷移（トリアージ）を持つ。他コンポーネント（運用計測の集計、境界判定の突合ミラー）もこのテーブルを共有し、独自テーブルは作らない。

補足:
- 同一 `fingerprint` を持つ `status='new'` 行が既存の場合、部分 UNIQUE インデックス（`fingerprint WHERE status='new'`）が競合を検知し、アプリ層は新規行を作らず `occurrence_count` を加算する。トリアージ済み（status が new 以外）の同型イベント再発は新規行になる
- タグ・リレーション・検索インデックス（search_index）・embeddingのいずれにも接続しない。habits と同様、事実上「観測ログ」として独立している
- `status` はライフサイクル列（§6）の `activities.status`（pending/in_progress/completed/snoozed/shelved）とは別の値集合（new/triaged/promoted/dismissed）を持つ

関連 migration: 0049_add_signal_events

カラム一覧・インデックス: `db-schema-tables.md` の `signal_events` 節参照。

### 3.22 relay_outbox

セッション間通信の publish（labels routing 配布）の送信キュー（transactional outbox）。`relay_publish` ツールが INSERT し、server 内の常駐配達ループが pending 行を relay サーバーへ配達する。at-least-once 保証は本テーブルだけで閉じる（relay サーバー側は永続真実を持たない）。

スキーマの単一の真実源は relay_sdk パッケージの DDL（`relay_sdk/outbox/schema.py`、relay リポジトリからの依存パッケージ）であり、migration 0056 はそれと同一形状を migration chain に組み込んだもの。

補足:
- タグ・リレーション・検索インデックス（search_index）・embeddingのいずれにも接続しない。通信レイヤ専用のキューであり、cc-memoryのエンティティモデルからは独立している
- SDK 側を再同期して DDL の形状が変わった場合は、新規 migration で追従する（0056 は事後改変しない）

関連 migration: 0056_add_relay_outbox

カラム一覧・インデックス: `db-schema-tables.md` の `relay_outbox` 節参照。

### 3.23 asks

人間の判断を待つ問いの記録先。signal_events と同様「双方の合意」が要らない受け皿だが、状態遷移（open→answered→promoted/dismissed、open→withdrawn）を持つため専用テーブルとして独立している。answer 時点ではトリアージ（promote/dismiss）を行わず、次の check_in で配達されるまで遅延する。

補足:
- `status='open'` の行に限り `fingerprint` を UNIQUE とする部分インデックスを張る。同一 fingerprint の open 行が既存なら INSERT は競合し、アプリ層は occurrence_count を加算する（signal_events と同じ dedup パターン、helperは `dedup_helpers` として共有）。トリアージ済み（answered/promoted/dismissed）・取り下げ済み（withdrawn）の同型問い再発は新規行になる
- CHECK制約で「open/withdrawn 以外は answer_body/answered_at 必須」「triage 設定は answered_at 必須」「promoted は promoted_decision_id 必須・それ以外は NULL 必須」「withdrawn は withdrawn_at 必須・それ以外は NULL 必須」を強制する
- `kind`（0068 追加）は `'ask'`（通常ask、既定）/`'meta'`（メタask）の2値のみ CHECK制約で固定する。メタaskは、同型の問いが繰り返され裁定が一貫していると判断されたときに `ask-distill` skill 経由で起票される「この型の問いを今後判例に従って自己裁定してよいか」を問う一段上のask。一般化ルールの発効は人間のメタask裁定でのみ行われる
- リレーション（related/belongs_to）には接続しない（v1では非対応）。タグは 0068 で `ask_tags` 経由の接続に対応した（§3.24 参照）が、topic からの継承（`get_effective_tags` 相当のUNION）はない点で decision/log とは異なる。近傍検索は ask_vec を経由する
- 文字列長上限（question 500字、context/answer_body 8000字）は DB 制約ではなくサービス層（`ask_service`）で強制する

関連 migration: 0062_add_asks（本体）、0068_add_asks_kind_and_tags（kind列 + ask_tags）

カラム一覧・インデックス: `db-schema-tables.md` の `asks` 節参照。

### 3.24 ask_blocks / ask_requesters / ask_tags

- `ask_blocks`: ask ↔ activity の junction（`PRIMARY KEY (ask_id, activity_id)`、両方 `ON DELETE CASCADE`）。このaskが答え待ちで止めているactivityを表す。answer/triage/withdrawのいずれの遷移でも該当askの行は削除される（blockの解除）
- `ask_requesters`: ask ↔ 要求元 `session_id` の junction（`PRIMARY KEY (ask_id, requester_session_id)`）。同じaskへの複数セッションからの要求をUNIONで蓄積する。withdraw時も削除しない（参照ログとして残す）
- `ask_tags`（0068 追加）: ask ↔ tag の junction。`decision_tags`/`material_tags`（§3.8）と全く同型（`PRIMARY KEY (ask_id, tag_id)`、両方 `ON DELETE CASCADE`）。`add_ask` はタグを必須（`domain:` タグを最低1つ含む）とし、`tag_service.resolve_tags` の完全一致・KNN統合を経て解決したタグIDをここに紐付ける。dedup時（同一fingerprintのopen ask再post）は今回渡されたタグを無視し、初回投入時の紐付けを保持する。既存31件（0068適用前のask）への遡及的タグ付与は行っていない

関連 migration: 0062_add_asks（ask_blocks / ask_requesters）、0068_add_asks_kind_and_tags（ask_tags）

カラム一覧・インデックス: `db-schema-tables.md` の `ask_blocks` 節・`ask_requesters` 節・`ask_tags` 節参照。

### 3.25 ask_vec

asks 専用の sqlite-vec 仮想テーブル（384次元、`distance_metric=cosine`）。topic_vec と同型で、rowid に `asks.id` を直接使う（vec_index のように search_index 経由の rowid 共有はしない）。asks は search_index/vec_index に参加しない（v1では検索対象外）ため、`add_ask` 時に生成した embedding をここにのみ格納する。embeddingサーバー未起動時は格納されない（近傍askサジェストが空配列になるだけで、ask自体の記録は成立する）。

関連 migration: 0062_add_asks

カラム一覧・インデックス: `db-schema-tables.md` の `ask_vec` 節参照。

### 3.26 injection_telemetry

記録系ツール（add_logs / add_decisions / add_material）が返す関連既存記録top3（記録=クエリ添付）について、「提示された記録が同セッションで実際に読まれたか」を機械記録する追随カウンタの present側（添付を返した瞬間）の台帳。取得側（fetch）は既存 `search_telemetry.results_json` / `fetch_telemetry.items_json` を再利用し、post-hoc の SQL 集計で `caller_session_id` を突合キーとして追随率を算出する（専用の集計ツールは持たない）。

補足:
- FK・UNIQUE制約は張らない（既存telemetryテーブル群と同じ、生データ台帳としての性質を優先）。同一セッションで同じ`(attached_type, attached_id)`が複数回提示されるのは正常挙動で、集計側で`GROUP BY MIN(timestamp)`して縮約する
- 書込は既存telemetryと同じdaemon thread + 失敗握りつぶし規約に従う
- 本 migration が導入する範囲は、テーブル定義・writable columns allowlist・present書込ヘルパ（`_record_injection_telemetry_async`）・`get_material`のfetch側計装のみ。`add_logs`/`add_decisions`/`add_material`側から実際にpresent行を書く呼出し実装は、添付内容の組み立て方を規定する記録=クエリ添付の詳細設計が別途確定してから追加する

関連 migration: 0067_add_injection_telemetry

カラム一覧・インデックス: `db-schema-tables.md` の `injection_telemetry` 節参照。

---

## 4. 関係メカニズム

エンティティ間関係は5系統に分散している。それぞれ表現方法が異なる。

| # | 系統 | 物理表現 | 対称性 | 種別 | 備考 |
|---|---|---|---|---|---|
| 1 | related | `relations`（ポリモーフィック、`relation_type='related'`） | 対称（CHECK で正規化） | エンティティ間の弱い関連 | 5エンティティ全組み合わせ可 |
| 2 | belongs_to（topic 帰属） | `relations`（ポリモーフィック、`relation_type='belongs_to'`） | 親→子（子が source） | 5エンティティ全て → topic | 0046 で decision/log の旧 `topic_id` FK も含めこの1系統に統一。partial index 2本あり（§3.9） |
| 3 | depends_on | `activity_dependencies` | 非対称（dependent → dependency） | activity 間のみ | 循環検出はアプリ層 |
| 4 | supersedes / destabilizes | `decision_supersedes`（`kind`列で区別） | 非対称（新 → 旧、または destabilize する側 → される側） | decision 間のみ | 循環検出は`kind`問わず合算判定（アプリ層）。destabilizesの解消記録は`decision_destabilization_resolutions`（§3.11a） |
| 5 | pin | `pins`（ポリモーフィック） | 非対称（source → target） | 任意エンティティ＋tag | 注意喚起・カタログ用 |

補足:
- 「トピックへの所属」は 0046（belongs_to 追加）/ 0047（旧 FK 物理削除）以前は (decision, log) と (activity, material) で異なる物理表現を取る非対称があったが、現在は全5エンティティが `relations.belongs_to` に統一されている（§8 の既知の課題3は解消済みとして記載更新）
- 関係系統が複数並走することは外部 API (`add_relation(relation_type=...)`) では隠蔽されているが、内部実装は系統ごとに別テーブルへ書き分ける

---

## 5. 検索インフラ

cc-memory の検索は以下3層の組み合わせで構成される。

### 5.1 search_index（中間テーブル）

5エンティティ（topic / activity / decision / log / material）を `(source_type, source_id)` で一意に識別する単一の中間テーブル。`id` カラムが FTS5 と vec_index の rowid を兼ねる。

### 5.2 search_index_fts（FTS5 全文検索）

contentless FTS5 仮想テーブル、trigram トークナイザ。`title` と `body` を全文検索する。

### 5.3 vec_index（ベクトル検索）

sqlite-vec の vec0 仮想テーブル、384次元の埋め込みを保持する。`search_index.id` と同じ rowid を共有する。

### 5.4 同期トリガー

各エンティティテーブルに INSERT / UPDATE / DELETE トリガーが定義され、search_index と search_index_fts へ同時書き込みする。テーブル定義変更（カラム追加・CHECK 制約変更）のたびにトリガーは DROP → 再 CREATE される（migration を遡ると 0002 / 0003 / 0005 / 0006 / 0007 / 0010 / 0011 / 0026 / 0027 / 0030 / 0037 / 0046（decisions/discussion_logs のtopic_id NULLABLE化に伴う再作成）と多数回再出現）。

retract 時は search_index / search_index_fts / vec_index（embedding_service経由）から該当エントリを同一SAVEPOINT内で物理削除する（`retract_service._delete_search_index_entry` + `embedding_service.delete_embedding_with_conn`）。§8 課題6(b) の「retractしてもsearch_indexの物理削除がない」という記述は古く、現在は物理削除される。

### 5.5 vec_index の整合性

vec_index は仮想テーブルのため FK 制約・トリガー連動が不可。embedding 生成・削除はアプリ層（embedding_service）でアトミックに行う規約となっている。

### 5.6 tag_vec（タグ専用 KNN）

tags テーブル用の独立 vec0 仮想テーブル。新規タグ作成時の表記ゆれ判定（KNN + 閾値）に使われる。

---

## 6. ライフサイクル列

| カラム | あるテーブル | ないテーブル | 意味 |
|---|---|---|---|
| `created_at` | 全テーブル | — | 作成時刻 |
| `updated_at` | activities / materials | topic / decision / log / habit | 更新時刻 |
| `retracted_at` | decisions / discussion_logs / materials | activities / discussion_topics / habits | 論理削除（取消し）時刻、NULL=有効 |
| `last_heartbeat_at` | activities | 他全テーブル | 最終ハートビート時刻 |
| `status` | activities | 他全テーブル | pending / in_progress / completed / snoozed / shelved |
| `active` | habits | 他全テーブル | 有効/無効フラグ（数値） |
| `caller_session_id`（廃止） | — | 全テーブル | 0048 で decisions/discussion_logs/discussion_topics/activities/materials に追加 → 0057 でcapability gating機構撤去に伴い削除済み |
| `pinned`（廃止） | — | 全テーブル | 0029 で decisions/logs/materials に追加 → 0035 で pins テーブル化により撤去済み |

補足:
- retracted_at は decision / log（0031）に加え material（0043）にもあり、topic / activity には存在しない
- retract 時は search_index / search_index_fts / vec_index を物理削除する（§5.4）ため `search()` はretract済みを自然に除外する。一方 `get_decisions` / `get_logs` 等、decisions/discussion_logs/materials テーブルを直接読む経路は search_index を経由しないため、引き続き `retracted_at IS NULL` によるフィルタが必要

---

## 7. マイグレーション履歴

| ファイル | 概要 |
|---|---|
| 0001_initial_schema | projects / discussion_topics / discussion_logs / decisions / tasks の初期5テーブル + インデックス |
| 0002_add_fts5_search | search_index + contentless FTS5 + topic / decision / task の同期トリガー新設 |
| 0003_project_to_subject | projects → subjects リネーム、project_id → subject_id、asana_url 削除、トリガー9本再作成 |
| 0004_fix_decisions_update_trigger | decision update トリガーの NULL ケース漏れバグ修正（1本 → 3本に分割） |
| 0005_add_vec_index | sqlite-vec の vec0 仮想テーブル新設（384次元） |
| 0005_decisions_topic_id_not_null | decisions.topic_id を NOT NULL 化、トリガーを再簡素化（**0005 番号重複**） |
| 0006_add_on_delete_cascade | discussion_logs / decisions の topic_id に ON DELETE CASCADE 追加 |
| 0007_remove_blocked_status | tasks.status の CHECK 制約から `blocked` 削除 |
| 0008_add_log_search_index | discussion_logs.title 追加 + 検索インデックス登録 |
| 0009_tag_infrastructure | tags / topic_tags / task_tags / decision_tags / log_tags / tag_vec 新設、subjects → domain:タグ移行 |
| 0010_remove_subjects | subjects 廃止、subject_id / parent_topic_id / tasks.topic_id カラム削除、トリガー12本書き直し |
| 0011_rename_task_to_activity | tasks → activities, task_tags → activity_tags リネーム、トリガー差し替え |
| 0012_add_tag_notes | tags.notes カラム追加 |
| 0013_add_materials | materials テーブル新設（activity_id FK 直結） |
| 0014_intent_namespace | scope: → 素タグ、mode: → intent: リネーム、intent:初期タグ投入、CHECK 制約更新 |
| 0015_intent_tag_notes | intent:* タグへ振る舞いガイド notes を投入 |
| 0015_tag_canonical | tags.canonical_id 追加（エイリアス用）（**0015 番号重複**） |
| 0016_add_activity_topic_id | activities.topic_id カラムを復活（0010 で削除されたもの） |
| 0017_add_heartbeat | activities.last_heartbeat_at 追加 |
| 0018_add_material_search_index | materials を search_index に登録、INSERT/UPDATE/DELETE トリガー新設 |
| 0019_add_reminders | reminders テーブル新設 |
| 0020_add_relation_tables | topic_relations / topic_activity_relations / activity_relations 新設 + relations_view（初版） |
| 0021_migrate_topic_id_to_relations | activities.topic_id を topic_activity_relations へ移行し、カラム削除 |
| 0022_add_detail_reminders | reminders に追加データを投入（運用ノウハウの文面） |
| 0023_material_independent_entity | material_tags / topic_material_relations / activity_material_relations 新設、materials.activity_id 削除、relations_view 拡張 |
| 0024_tag_description | tags.description 追加、intent:debug 新設、intent:* description 設定 |
| 0025_rename_reminders_to_habits | reminders → habits リネーム |
| 0026_add_snoozed_status | activities.status CHECK 制約に snoozed 追加、トリガー3本再生成 |
| 0027_add_shelved_status | activities.status CHECK 制約に shelved 追加、トリガー3本再生成 |
| 0028_add_activity_dependencies | activity_dependencies 新設、relations_view 拡張（depends_on 追加） |
| 0029_add_pinned | discussion_logs / decisions / materials に pinned BOOLEAN カラム追加 |
| 0030_add_search_index_created_at | search_index.created_at 追加 + 5エンティティ INSERT トリガー再生成 |
| 0031_add_retracted_at | decisions / discussion_logs に retracted_at 追加 |
| 0032_add_material_source | materials.source カラム追加（DEFAULT `'unknown'`） |
| 0033_relation_expansion | 旧5 relation テーブルを relations 単一テーブルに統合、decision_supersedes 新設、CASCADE トリガー5本、relations_view 再構築 |
| 0034_pins_directed_relation | pins テーブル新設、pinned=1 material を pins へ移行 |
| 0035_drop_pinned_columns | discussion_logs / decisions / materials の pinned カラム削除 |
| 0036_add_materials_updated_at | materials.updated_at 追加（既存行は created_at でバックフィル） |
| 0037_add_decisions_title | decisions.title 追加、search_index トリガーで `COALESCE(title, decision)` 表示 |
| 0038_pins_target_index_and_cascade | pins(target_type, target_id) インデックス追加、pins CASCADE トリガー6本（topic / activity / material / decision / log / tag）追加 |
| 0039_extend_tag_namespace | tags の namespace CHECK 制約撤廃（テーブル再構築）、妥当性は Python 層検証へ |
| 0039_intent_thinking | intent:thinking タグ新設、description / notes 設定（**0039 番号重複**） |
| 0040_add_heartbeat_session_id | activities.last_heartbeat_session_id 追加（自セッションheartbeatの誤表示解消） |
| 0041_add_search_telemetry | search_telemetry テーブル新設（§3.18） |
| 0042_citations_table | citations テーブル新設 + owner側cascade削除トリガー5本（§3.19） |
| 0043_add_materials_retracted_at | materials.retracted_at 追加（decision/log 同様の retract 機構を対称化） |
| 0044_sanitize_log_table | sanitize_log テーブル新設（INSERT経路未実装のまま据置。0046で作り直し） |
| 0045_add_activities_orch_managed | activities.orch_managed 追加（素タグ `orch-managed` からの構造的属性昇格 + データ移行） |
| 0046_relations_belongs_to_unify | relations.relation_type CHECK を `('related','belongs_to')` に緩和、partial index 2本追加、既存 material/activity/decision/log→topic の `related` 行を `belongs_to` に変換、decisions/discussion_logs.topic_id を relations.belongs_to へ複製したうえで NULLABLE 化・FK 削除（トリガー再作成込み） |
| 0046_sanitize_log_to_citation_event_log | sanitize_log を DROP し citation_event_log として作り直し（逐次行型 + VIEW 3本、§3.20）（**0046 番号重複**） |
| 0047_drop_decisions_logs_topic_id | decisions.topic_id / discussion_logs.topic_id カラムを物理削除（0046で確保した前提条件を受けての Contract） |
| 0048_session_identity | session_identity テーブル新設 + decisions/discussion_logs/discussion_topics/activities/materials に caller_session_id 追加（0057で全て削除） |
| 0057_drop_capability_gating | session_identity テーブル削除 + decisions/discussion_logs/discussion_topics/activities/materials の caller_session_id カラム削除（role-based capability gating機構の呼び出し元解体に伴う撤去） |
| 0058_add_habit_trigger_mode | habits に description / trigger_mode / importance_score / last_recalled_at を追加（スキーマ変更のみ、データ移行なし。trigger_modeの切り替えはupdate_habit経由で個別適用） |
| 0059_add_habit_status | habits に status（'active'/'archived'、既定'active'）を追加 |
| 0060_add_habit_importance_score_check | trigger_mode='intelligently'かつimportance_score=1.0(未設定)のhabitを3に補正したうえで、importance_scoreにCHECK(IN (1, 2, 3))を追加（テーブル再構築） |
| 0061_add_tag_archived | tags に archived_at（退役日時）/ archived_reason（退役理由、100文字以内のCHECK制約付き）を追加、archived_at 用の部分インデックス idx_tags_archived_at を新設（スキーマ変更のみ、データ移行なし） |
| 0062_add_asks | asks / ask_blocks / ask_requesters テーブル新設 + ask専用 vec0 仮想テーブル ask_vec 新設（§3.23-3.25） |
| 0063_add_decision_supersedes_kind | decision_supersedes に kind 列（'replaces'/'destabilizes'）追加（テーブル再構築、PK に kind を含める形へ変更）、decision_destabilization_resolutions テーブル新設、relations_view の supersedes 由来行を kind で出し分け（§3.11, §3.11a, §3.17） |
| 0064_add_tags_last_injected_at | tags に last_injected_at（tag notes 全文配信の最終実績日時、既定NULL）を追加。レンダー時decay述語（`is_decay_eligible`）の入力として使う（スキーマ変更のみ、データ移行なし） |
| 0065_add_habits_always_pool_ratchet_trigger | trigger_mode='always'かつactive=1なhabitのcontent合計文字数が2000字を超えて増加するINSERT/UPDATEをRAISE(ABORT)で拒否するトリガー2本を新設（ラチェット型天井、縮む変更は常に許可） |
| 0066_add_tags_notes_ratchet_trigger | tags.notesが4000字を超えて増加するINSERT/UPDATEをRAISE(ABORT)で拒否するトリガー2本を新設（1タグあたりのラチェット型天井、縮む変更は常に許可） |
| 0067_add_injection_telemetry | injection_telemetry テーブル新設（記録=クエリ添付の追随カウンタ present側台帳、§3.26） |
| 0068_add_asks_kind_and_tags | asks に kind 列（'ask'/'meta'、既定'ask'）を追加、ask_tags junction テーブル新設（§3.23, §3.24） |

重複番号: **0005** （add_vec_index / decisions_topic_id_not_null）、**0015** （intent_tag_notes / tag_canonical）、**0039** （extend_tag_namespace / intent_thinking）、**0046** （relations_belongs_to_unify / sanitize_log_to_citation_event_log）。yoyo は depends 宣言で順序を解決するため運用上は機能するが、ファイル名上の連番ユニーク性が崩れている。

---

## 8. 既知の課題

5次元統合レポート（material 312 / 239）の T1 エンティティモデル & データアーキ次元で指摘された事実の書き起こし。設計議論用のチェックリストとして残す。

1. **FTS5 同期トリガーの手書き重複**: 5エンティティ × 3トリガーが、スキーマ変更（CHECK 制約変更・カラム追加）のたびに DROP → 再 CREATE され、同じロジックが migration 0002 / 0003 / 0005 / 0006 / 0007 / 0010 / 0011 / 0026 / 0027 / 0030 / 0037 で10回以上重複再出現する。0004 はそのトリガーの NULL ケース漏れバグの修正であり、手書き重複がバグの温床となった事例である。

2. **decision と log の構造同型コピペ**: 両テーブルは「2テキストフィールド + retracted_at + タグ + FTS 登録」と構造同型（旧 `topic_id FK(NOT NULL)` は0046/0047で撤去され、現在は両テーブルとも relations.belongs_to 経由）。サービス層の集約取得も逐語コピペで実装されており、改修時に2箇所を同期する必要がある。

3. **〔0046/0047 で解消済み〕親子帰属表現の FK / relation 分裂**: 「トピックへの所属」という同じ意味論を、decision / log は `topic_id` FK で、activity / material は `relations` テーブルで表現する非対称がかつて存在した。0046 で `relations.relation_type` に `belongs_to` を追加し、decision/log の `topic_id` を belongs_to へ複製したうえで NULLABLE 化、0047 でカラム自体を物理削除した。現在は5エンティティ全てが `relations.belongs_to` に統一されている（§4）。

4. **関係メカニズムの5系統並走**: `relations`（related）/ `relations`（belongs_to）/ `activity_dependencies` / `decision_supersedes` / `pins` の5系統が並走する（§4）。`relations.relation_type` は0046以降 `('related', 'belongs_to')` の2値を持ち、もはやデッドカラムではない。

5. **ポリモーフィック FK 制約不可**: `relations` / `pins` / `vec_index` はポリモーフィックまたは仮想テーブルのため DB の FK 制約が張れない。CASCADE 削除は親テーブルの AFTER DELETE トリガー（relations 5本 + pins 6本）で手動実装。vec_index の孤児削除はアプリ層（embedding_service）が担う。

6. **retract / supersedes ライフサイクルの未閉鎖**: (a) supersedes（新→旧）を張っても旧 decision は自動 retract されない。(b) 〔0043 以前の記述、解消済み〕retract 時は search_index / search_index_fts / vec_index を物理削除するようになった（§5.4）。ただし decisions/discussion_logs/materials 本体の行は残るため、これらを直接読む経路（get_decisions等）は引き続き `retracted_at IS NULL` フィルタが必要。(c) 〔一部解消〕retracted_at は decision / log（0031）に加え material（0043）にもあるが、topic / activity には存在しない。

7. **habit エンティティの孤立**: habits は `content + active + created_at + description + trigger_mode + importance_score + last_recalled_at` を持つが、タグ・embedding・relation・search_index のいずれにも接続しない。他5エンティティと並べる位置づけにはなっていない。

8. **タグ解決 `resolve_tags()` のアトミック性欠如**: tag_service の `resolve_tags()` はループ内で中間 commit を行うため、複数タグ処理途中のエラーで前半 INSERT がロールバックされず中途半端な状態が残る。

9. **スキーマ進化のデザインデット**: migration 番号の重複（0005×2 / 0015×2 / 0039×2 / 0046×2）と、materials の高頻度改修（0013 / 0018 / 0023 / 0029 / 0032 / 0034 / 0035 / 0036 / 0043 / 0048 / 0057 で計11回）。

10. **`update_tag()` の単一関数4操作**: tag_service の `update_tag()` は rename / notes / canonical / description の4種を1関数に集約し if 分岐している。

---

## 9. 未確認事項

本ドキュメント作成時点でコード・migration から確証が取れなかった項目。

- 各テーブルの実 row 数感（DB 実体の統計）
- vec_index / tag_vec の384次元値が使う embedding モデル名（コード側 embedding_service に固定されている想定だが本ドキュメントでは未確認）
- 0021 で activity の topic_id を relations 化したが、relations 側に旧 topic_id 由来データが残っているかの実データ確認（コード上は INSERT OR IGNORE で移行）
