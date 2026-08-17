# CALM DB スキーマ自動ダンプ

<!-- 自動生成ファイル。手動編集しないこと。 -->
<!-- 生成元: scripts/dump_db_schema.py（migrations/ 全適用後の実スキーマから生成） -->
<!-- 再生成: uv run python scripts/dump_db_schema.py -->

`migrations/` を通し番号順に全適用した結果として得られる、現在のテーブル/ビュー構造の機械的な写しである。
カラム名・型・NULL可否・デフォルト値・インデックスは常に本ファイルが最新（生成時点で最新migrationは 0070）。

「なぜこの形なのか」（設計判断の背景・変遷・既知の課題）は `docs/spec/db-schema.md` を参照。
本ファイルは現在値のみを扱い、変遷の経緯（旧カラムの削除理由等）は記載しない。

---

### activities

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| title | VARCHAR(255) | NO | — | — |
| description | TEXT | NO | — | — |
| status | VARCHAR(20) | NO | `'pending'` | — |
| created_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |
| updated_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |
| last_heartbeat_at | TEXT | YES | — | — |
| last_heartbeat_session_id | TEXT | YES | — | — |
| orch_managed | BOOLEAN | NO | `0` | — |

インデックス:
- `idx_activities_status` ON `activities`(status)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "activities" (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title VARCHAR(255) NOT NULL,
  description TEXT NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'in_progress', 'completed', 'snoozed', 'shelved')),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_heartbeat_at TEXT
, last_heartbeat_session_id TEXT, orch_managed BOOLEAN NOT NULL DEFAULT 0)
```

</details>

### activity_dependencies

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| dependent_id | INTEGER | NO | — | PK |
| dependency_id | INTEGER | NO | — | PK |
| created_at | TEXT | YES | `datetime('now')` | — |

インデックス:
- `idx_activity_dependencies_dependency` ON `activity_dependencies`(dependency_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE activity_dependencies (
    dependent_id  INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    dependency_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    created_at    TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (dependent_id, dependency_id),
    CHECK (dependent_id != dependency_id)
)
```

</details>

### activity_tags

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| activity_id | INTEGER | NO | — | PK |
| tag_id | INTEGER | NO | — | PK |

インデックス:
- `idx_activity_tags_tag` ON `activity_tags`(tag_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "activity_tags" (
  activity_id INTEGER NOT NULL REFERENCES "activities"(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (activity_id, tag_id)
)
```

</details>

### ask_blocks

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| ask_id | INTEGER | NO | — | PK |
| activity_id | INTEGER | NO | — | PK |
| added_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |

インデックス:
- `idx_ask_blocks_activity` ON `ask_blocks`(activity_id, ask_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE ask_blocks (
    ask_id      INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
    added_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ask_id, activity_id),
    FOREIGN KEY (ask_id) REFERENCES asks(id) ON DELETE CASCADE,
    FOREIGN KEY (activity_id) REFERENCES activities(id) ON DELETE CASCADE
)
```

</details>

### ask_requesters

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| ask_id | INTEGER | NO | — | PK |
| requester_session_id | TEXT | NO | — | PK |
| added_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE ask_requesters (
    ask_id                INTEGER NOT NULL,
    requester_session_id  TEXT NOT NULL,
    added_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ask_id, requester_session_id),
    FOREIGN KEY (ask_id) REFERENCES asks(id) ON DELETE CASCADE
)
```

</details>

### ask_tags

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| ask_id | INTEGER | NO | — | PK |
| tag_id | INTEGER | NO | — | PK |

インデックス:
- `idx_ask_tags_tag` ON `ask_tags`(tag_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE ask_tags (
    ask_id  INTEGER NOT NULL,
    tag_id  INTEGER NOT NULL,
    PRIMARY KEY (ask_id, tag_id),
    FOREIGN KEY (ask_id) REFERENCES asks(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
)
```

</details>

### ask_vec

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| rowid | — | YES | — | — |
| embedding | — | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE VIRTUAL TABLE ask_vec USING vec0(
  embedding float[384] distance_metric=cosine
)
```

</details>

### ask_vec_chunks

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| chunk_id | INTEGER | NO | — | PK |
| size | INTEGER | NO | — | — |
| validity | BLOB | NO | — | — |
| rowids | BLOB | NO | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "ask_vec_chunks"(chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,size INTEGER NOT NULL,validity BLOB NOT NULL,rowids BLOB NOT NULL)
```

</details>

### ask_vec_info

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| key | TEXT | NO | — | PK |
| value | ANY | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "ask_vec_info" (key text primary key, value any)
```

</details>

### ask_vec_rowids

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| rowid | INTEGER | NO | — | PK |
| id | — | YES | — | — |
| chunk_id | INTEGER | YES | — | — |
| chunk_offset | INTEGER | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "ask_vec_rowids"(rowid INTEGER PRIMARY KEY AUTOINCREMENT,id,chunk_id INTEGER,chunk_offset INTEGER)
```

</details>

### ask_vec_vector_chunks00

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| rowid | — | NO | — | PK |
| vectors | BLOB | NO | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "ask_vec_vector_chunks00"(rowid PRIMARY KEY,vectors BLOB NOT NULL)
```

</details>

### asks

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| question | TEXT | NO | — | — |
| context | TEXT | YES | — | — |
| fingerprint | TEXT | NO | — | — |
| status | TEXT | NO | `'open'` | — |
| answer_body | TEXT | YES | — | — |
| answered_at | TIMESTAMP | YES | — | — |
| answered_session_id | TEXT | YES | — | — |
| triage | TEXT | YES | — | — |
| triaged_at | TIMESTAMP | YES | — | — |
| triaged_session_id | TEXT | YES | — | — |
| triage_reason | TEXT | YES | — | — |
| promoted_decision_id | INTEGER | YES | — | — |
| withdrawn_at | TIMESTAMP | YES | — | — |
| withdrawn_session_id | TEXT | YES | — | — |
| withdraw_reason | TEXT | YES | — | — |
| occurrence_count | INTEGER | NO | `1` | — |
| first_seen_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |
| last_seen_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |
| first_seen_session_id | TEXT | YES | — | — |
| last_seen_session_id | TEXT | YES | — | — |
| kind | TEXT | NO | `'ask'` | — |
| choices | TEXT | YES | — | — |

インデックス:
- `idx_asks_triage_pending` ON `asks`(last_seen_at)
- `idx_asks_status_last_seen` ON `asks`(status, last_seen_at)
- `idx_asks_fingerprint_open` UNIQUE ON `asks`(fingerprint)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE asks (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    question               TEXT NOT NULL,
    context                TEXT,
    fingerprint            TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'open'
                           CHECK (status IN ('open','answered','promoted','dismissed','withdrawn')),

    answer_body            TEXT,
    answered_at            TIMESTAMP NULL,
    answered_session_id    TEXT,

    triage                 TEXT
                           CHECK (triage IS NULL OR triage IN ('promote','dismiss')),
    triaged_at             TIMESTAMP NULL,
    triaged_session_id     TEXT,
    triage_reason          TEXT,
    promoted_decision_id   INTEGER,

    withdrawn_at           TIMESTAMP NULL,
    withdrawn_session_id   TEXT,
    withdraw_reason        TEXT,

    occurrence_count       INTEGER NOT NULL DEFAULT 1,
    first_seen_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    first_seen_session_id  TEXT,
    last_seen_session_id   TEXT, kind TEXT NOT NULL DEFAULT 'ask'
    CHECK (kind IN ('ask', 'meta')), choices TEXT,

    CHECK (
        (status IN ('open','withdrawn'))
        OR (answer_body IS NOT NULL AND answered_at IS NOT NULL)
    ),
    CHECK (
        (triage IS NULL) OR (answered_at IS NOT NULL)
    ),
    CHECK (
        (status = 'promoted' AND promoted_decision_id IS NOT NULL)
        OR (status <> 'promoted' AND promoted_decision_id IS NULL)
    ),
    CHECK (
        (status = 'withdrawn' AND withdrawn_at IS NOT NULL)
        OR (status <> 'withdrawn' AND withdrawn_at IS NULL)
    ),

    FOREIGN KEY (promoted_decision_id) REFERENCES decisions(id)
)
```

</details>

### auto_convert_event_log

VIEW。定義SQL:

```sql
CREATE VIEW auto_convert_event_log AS
SELECT *
FROM citation_event_log
WHERE source IN ('write_auto_convert', 'bulk_migration')
```

### citation_event_log

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| occurred_at | TEXT | NO | `datetime('now')` | — |
| source | TEXT | NO | — | — |
| tool_name | TEXT | YES | — | — |
| target_entity_type | TEXT | YES | — | — |
| target_entity_id | INTEGER | YES | — | — |
| target_field | TEXT | YES | — | — |
| before_text | TEXT | NO | — | — |
| after_text | TEXT | NO | — | — |
| verified_at | TEXT | YES | — | — |
| verification_result | TEXT | YES | — | — |
| extra_json | TEXT | YES | — | — |

インデックス:
- `idx_citation_event_log_occurred_at` ON `citation_event_log`(occurred_at)
- `idx_citation_event_log_source` ON `citation_event_log`(source)
- `idx_citation_event_log_target` ON `citation_event_log`(target_entity_type, target_entity_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE citation_event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL CHECK (source IN (
        'write_auto_convert',
        'bulk_migration',
        'transcript_post_tool_use',
        'transcript_session_start_backfill',
        'external_doc_sanitize'
    )),
    tool_name TEXT,
    target_entity_type TEXT CHECK (target_entity_type IS NULL OR target_entity_type IN (
        'decision', 'activity', 'log', 'material', 'topic'
    )),
    target_entity_id INTEGER,
    target_field TEXT,
    before_text TEXT NOT NULL,
    after_text TEXT NOT NULL,
    verified_at TEXT,
    verification_result TEXT CHECK (verification_result IS NULL OR verification_result IN (
        'exists', 'dangling', 'skip'
    )),
    extra_json TEXT
)
```

</details>

### citation_event_log_by_entity

VIEW。定義SQL:

```sql
CREATE VIEW citation_event_log_by_entity AS
SELECT
    target_entity_type,
    target_entity_id,
    COUNT(*) AS event_count,
    MAX(occurred_at) AS last_occurred_at
FROM citation_event_log
WHERE target_entity_type IS NOT NULL
  AND target_entity_id IS NOT NULL
GROUP BY target_entity_type, target_entity_id
```

### citations

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| owner_type | TEXT | NO | — | — |
| owner_id | INTEGER | NO | — | — |
| target_type | TEXT | NO | — | — |
| target_id | INTEGER | NO | — | — |
| occurrence | INTEGER | NO | — | — |
| created_at | TEXT | YES | `datetime('now')` | — |

インデックス:
- `idx_citations_owner` ON `citations`(owner_type, owner_id)
- `idx_citations_target` ON `citations`(target_type, target_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT NOT NULL CHECK(owner_type IN ('material', 'decision', 'log', 'activity', 'topic')),
    owner_id INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK(target_type IN ('material', 'decision', 'log', 'activity', 'topic')),
    target_id INTEGER NOT NULL,
    occurrence INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(owner_type, owner_id, occurrence)
)
```

</details>

### decision_destabilization_resolutions

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| source_id | INTEGER | NO | — | PK |
| target_id | INTEGER | NO | — | PK |
| resolution | TEXT | NO | — | — |
| revised_to_decision_id | INTEGER | YES | — | — |
| note | TEXT | NO | `''` | — |
| resolved_at | TEXT | NO | `datetime('now')` | — |

インデックス:
- `idx_destab_resolutions_target` ON `decision_destabilization_resolutions`(target_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE decision_destabilization_resolutions (
    source_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    resolution TEXT NOT NULL
        CHECK (resolution IN ('reaffirmed', 'revised', 'retracted')),
    revised_to_decision_id INTEGER NULL REFERENCES decisions(id) ON DELETE SET NULL,
    note TEXT NOT NULL DEFAULT '',
    resolved_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_id, target_id)
)
```

</details>

### decision_supersedes

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| source_id | INTEGER | NO | — | PK |
| target_id | INTEGER | NO | — | PK |
| kind | TEXT | NO | `'replaces'` | PK |
| created_at | TEXT | YES | `datetime('now')` | — |

インデックス:
- `idx_decision_supersedes_target` ON `decision_supersedes`(target_id, kind)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE decision_supersedes (
    source_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'replaces'
        CHECK (kind IN ('replaces', 'destabilizes')),
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (source_id, target_id, kind),
    CHECK (source_id != target_id)
)
```

</details>

### decision_tags

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| decision_id | INTEGER | NO | — | PK |
| tag_id | INTEGER | NO | — | PK |

インデックス:
- `idx_decision_tags_tag` ON `decision_tags`(tag_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE decision_tags (
  decision_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (decision_id, tag_id)
)
```

</details>

### decisions

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| decision | TEXT | NO | — | — |
| reason | TEXT | NO | — | — |
| created_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |
| retracted_at | TIMESTAMP | YES | — | — |
| title | TEXT | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  -- topic_id は 0047 で物理削除予定。当面は NULL 許容で残置 (旧 INSERT パスの後方互換ではなく、
  -- migration 適用直後の中間状態で SELECT 互換性を維持するための残置)
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  retracted_at TIMESTAMP NULL,
  title TEXT
)
```

</details>

### discussion_logs

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| content | TEXT | NO | — | — |
| created_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |
| title | TEXT | NO | `''` | — |
| retracted_at | TIMESTAMP | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE discussion_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  title TEXT NOT NULL DEFAULT '',
  retracted_at TIMESTAMP NULL
)
```

</details>

### discussion_topics

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| title | VARCHAR(255) | NO | — | — |
| description | TEXT | NO | — | — |
| created_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE discussion_topics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title VARCHAR(255) NOT NULL,
  description TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
```

</details>

### fetch_telemetry

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| tool | TEXT | NO | — | — |
| items_json | TEXT | NO | — | — |
| caller_session_id | TEXT | YES | — | — |
| timestamp | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |

インデックス:
- `idx_fetch_telemetry_timestamp` ON `fetch_telemetry`(timestamp)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE fetch_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL,
    items_json TEXT NOT NULL,
    caller_session_id TEXT,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
```

</details>

### habits

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| content | TEXT | NO | — | — |
| active | INTEGER | NO | `1` | — |
| created_at | TEXT | YES | `datetime('now')` | — |
| description | TEXT | NO | `''` | — |
| trigger_mode | TEXT | NO | `'always'` | — |
| importance_score | REAL | NO | `1.0` | — |
| last_recalled_at | TIMESTAMP | YES | — | — |
| status | TEXT | NO | `'active'` | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "habits" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    description TEXT NOT NULL DEFAULT '',
    trigger_mode TEXT NOT NULL DEFAULT 'always' CHECK(trigger_mode IN ('always', 'intelligently')),
    importance_score REAL NOT NULL DEFAULT 1.0 CHECK(importance_score IN (1, 2, 3)),
    last_recalled_at TIMESTAMP NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived'))
)
```

</details>

### injection_telemetry

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| caller_session_id | TEXT | YES | — | — |
| trigger_tool | TEXT | NO | — | — |
| source_type | TEXT | NO | — | — |
| source_id | INTEGER | NO | — | — |
| attached_type | TEXT | NO | — | — |
| attached_id | INTEGER | NO | — | — |
| rank | INTEGER | NO | — | — |
| similarity | REAL | YES | — | — |
| diagnostics_json | TEXT | YES | — | — |
| timestamp | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |

インデックス:
- `idx_injection_telemetry_attached` ON `injection_telemetry`(attached_type, attached_id)
- `idx_injection_telemetry_session_ts` ON `injection_telemetry`(caller_session_id, timestamp)

<details><summary>CREATE文（生成元migration）</summary>

```sql
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
)
```

</details>

### instance_meta

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| instance_id | TEXT | NO | — | — |
| created_at | TEXT | NO | `datetime('now')` | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE instance_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    instance_id TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
)
```

</details>

### log_tags

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| log_id | INTEGER | NO | — | PK |
| tag_id | INTEGER | NO | — | PK |

インデックス:
- `idx_log_tags_tag` ON `log_tags`(tag_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE log_tags (
  log_id INTEGER NOT NULL REFERENCES discussion_logs(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (log_id, tag_id)
)
```

</details>

### material_tags

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| material_id | INTEGER | NO | — | PK |
| tag_id | INTEGER | NO | — | PK |

インデックス:
- `idx_material_tags_tag` ON `material_tags`(tag_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE material_tags (
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (material_id, tag_id)
)
```

</details>

### materials

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| title | TEXT | NO | — | — |
| content | TEXT | NO | — | — |
| created_at | TEXT | NO | `strftime('%Y-%m-%d %H:%M:%S', 'now')` | — |
| source | TEXT | NO | `'unknown'` | — |
| updated_at | TIMESTAMP | YES | — | — |
| retracted_at | TIMESTAMP | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
, source TEXT NOT NULL DEFAULT 'unknown', updated_at TIMESTAMP, retracted_at TIMESTAMP NULL)
```

</details>

### migration_ledger

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| migration_id | TEXT | NO | — | PK |
| content_sha256 | TEXT | NO | — | — |
| applied_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE migration_ledger (
    migration_id   TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL,
    applied_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
```

</details>

### pins

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| source_type | TEXT | NO | — | PK |
| source_id | INTEGER | NO | — | PK |
| target_type | TEXT | NO | — | PK |
| target_id | INTEGER | NO | — | PK |
| created_at | TEXT | YES | `datetime('now')` | — |

インデックス:
- `idx_pins_target` ON `pins`(target_type, target_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE pins (
    source_type TEXT NOT NULL CHECK(source_type IN ('tag','activity','topic','decision','log','material')),
    source_id   INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK(target_type IN ('tag','activity','topic','decision','log','material')),
    target_id   INTEGER NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (source_type, source_id, target_type, target_id)
)
```

</details>

### precedent_telemetry

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| context | TEXT | NO | — | — |
| parameters | TEXT | NO | — | — |
| guarantee | TEXT | NO | — | — |
| routing_json | TEXT | NO | — | — |
| decisions_total | INTEGER | NO | — | — |
| full_count | INTEGER | NO | — | — |
| timestamp | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |

インデックス:
- `idx_precedent_telemetry_timestamp` ON `precedent_telemetry`(timestamp)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE precedent_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    context TEXT NOT NULL,
    parameters TEXT NOT NULL,
    guarantee TEXT NOT NULL,
    routing_json TEXT NOT NULL,
    decisions_total INTEGER NOT NULL,
    full_count INTEGER NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
```

</details>

### relations

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| source_type | TEXT | NO | — | PK |
| source_id | INTEGER | NO | — | PK |
| target_type | TEXT | NO | — | PK |
| target_id | INTEGER | NO | — | PK |
| relation_type | TEXT | NO | `'related'` | — |
| created_at | TEXT | YES | `datetime('now')` | — |

インデックス:
- `idx_relations_belongs_to_src` ON `relations`(source_type, source_id, target_id)
- `idx_relations_belongs_to_tgt` ON `relations`(target_type, target_id, source_type, source_id)
- `idx_relations_target` ON `relations`(target_type, target_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE relations (
    source_type TEXT NOT NULL CHECK(source_type IN ('topic', 'activity', 'material', 'decision', 'log')),
    source_id INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK(target_type IN ('topic', 'activity', 'material', 'decision', 'log')),
    target_id INTEGER NOT NULL,
    -- 'related' は対称関係、'belongs_to' は子→親 (decision/log/material/activity → topic) を表す
    relation_type TEXT NOT NULL DEFAULT 'related' CHECK(relation_type IN ('related', 'belongs_to')),
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (source_type, source_id, target_type, target_id),
    CHECK (source_type < target_type OR (source_type = target_type AND source_id < target_id))
)
```

</details>

### relations_view

VIEW。定義SQL:

```sql
CREATE VIEW relations_view AS
  SELECT source_type, source_id, target_type, target_id, relation_type, created_at
  FROM relations
  UNION ALL
  SELECT target_type, target_id, source_type, source_id, relation_type, created_at
  FROM relations
  UNION ALL
  SELECT 'activity' AS source_type, dependent_id AS source_id,
         'activity' AS target_type, dependency_id AS target_id,
         'depends_on' AS relation_type, created_at
  FROM activity_dependencies
  UNION ALL
  SELECT 'decision' AS source_type, source_id,
         'decision' AS target_type, target_id,
         CASE kind
              WHEN 'replaces' THEN 'supersedes'
              WHEN 'destabilizes' THEN 'destabilizes'
         END AS relation_type,
         created_at
  FROM decision_supersedes
```

### relay_outbox

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| ref_type | TEXT | NO | — | — |
| ref_id | TEXT | NO | — | — |
| labels | TEXT | NO | — | — |
| title | TEXT | YES | — | — |
| idempotency_key | TEXT | NO | — | — |
| created_at | TEXT | NO | — | — |
| processed_at | TEXT | YES | — | — |
| retry_count | INTEGER | NO | `0` | — |
| last_error | TEXT | YES | — | — |
| dead_at | TEXT | YES | — | — |

インデックス:
- `idx_relay_outbox_pending` ON `relay_outbox`(id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE relay_outbox (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ref_type        TEXT    NOT NULL,
  ref_id          TEXT    NOT NULL,
  labels          TEXT    NOT NULL,             -- JSON array
  title           TEXT,
  idempotency_key TEXT    NOT NULL,             -- SDK が auto-generate（id を流用）
  created_at      TEXT    NOT NULL,             -- ISO8601 UTC
  processed_at    TEXT,                         -- NULL = pending
  retry_count     INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  dead_at         TEXT                          -- NOT NULL = DLQ 行き
)
```

</details>

### sanitize_event_log

VIEW。定義SQL:

```sql
CREATE VIEW sanitize_event_log AS
SELECT *
FROM citation_event_log
WHERE source IN (
    'transcript_post_tool_use',
    'transcript_session_start_backfill',
    'external_doc_sanitize'
)
```

### search_index

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| source_type | TEXT | NO | — | — |
| source_id | INTEGER | NO | — | — |
| title | TEXT | NO | `''` | — |
| created_at | TEXT | NO | `''` | — |

インデックス:
- `idx_search_index_created_at` ON `search_index`(created_at)
- `idx_search_index_source` ON `search_index`(source_type, source_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE search_index (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_type TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '',
  UNIQUE(source_type, source_id)
)
```

</details>

### search_index_fts

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| title | — | YES | — | — |
| body | — | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE VIRTUAL TABLE search_index_fts USING fts5(
  title,
  body,
  content='',
  tokenize='trigram'
)
```

</details>

### search_index_fts_config

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| k | — | NO | — | PK |
| v | — | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE 'search_index_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID
```

</details>

### search_index_fts_data

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| block | BLOB | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE 'search_index_fts_data'(id INTEGER PRIMARY KEY, block BLOB)
```

</details>

### search_index_fts_docsize

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| sz | BLOB | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE 'search_index_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB)
```

</details>

### search_index_fts_idx

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| segid | — | NO | — | PK |
| term | — | NO | — | PK |
| pgno | — | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE 'search_index_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID
```

</details>

### search_telemetry

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| query | TEXT | NO | — | — |
| parameters | TEXT | NO | — | — |
| result_count | INTEGER | NO | — | — |
| timestamp | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |
| results_json | TEXT | YES | — | — |
| diagnostics_json | TEXT | YES | — | — |
| caller_session_id | TEXT | YES | — | — |

インデックス:
- `idx_search_telemetry_timestamp` ON `search_telemetry`(timestamp)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE search_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    parameters TEXT NOT NULL,
    result_count INTEGER NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
, results_json TEXT, diagnostics_json TEXT, caller_session_id TEXT)
```

</details>

### signal_events

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| kind | TEXT | NO | — | — |
| source | TEXT | NO | — | — |
| summary | TEXT | NO | — | — |
| detail | TEXT | YES | — | — |
| refs | TEXT | YES | — | — |
| context | TEXT | YES | — | — |
| fingerprint | TEXT | NO | — | — |
| occurrence_count | INTEGER | NO | `1` | — |
| first_seen_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |
| last_seen_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |
| session_id | TEXT | YES | — | — |
| status | TEXT | NO | `'new'` | — |
| promoted_type | TEXT | YES | — | — |
| promoted_id | INTEGER | YES | — | — |

インデックス:
- `idx_signal_kind` ON `signal_events`(kind, last_seen_at)
- `idx_signal_status` ON `signal_events`(status, last_seen_at)
- `idx_signal_fingerprint_new` UNIQUE ON `signal_events`(fingerprint)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE signal_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,
    source           TEXT NOT NULL,
    summary          TEXT NOT NULL,
    detail           TEXT,
    refs             TEXT,
    context          TEXT,
    fingerprint      TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    session_id       TEXT,
    status           TEXT NOT NULL DEFAULT 'new'
                     CHECK (status IN ('new', 'triaged', 'promoted', 'dismissed')),
    promoted_type    TEXT,
    promoted_id      INTEGER,
    CHECK ((promoted_type IS NULL) = (promoted_id IS NULL))
)
```

</details>

### tag_vec

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| rowid | — | YES | — | — |
| embedding | — | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE VIRTUAL TABLE tag_vec USING vec0(
  embedding float[384]
)
```

</details>

### tag_vec_chunks

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| chunk_id | INTEGER | NO | — | PK |
| size | INTEGER | NO | — | — |
| validity | BLOB | NO | — | — |
| rowids | BLOB | NO | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "tag_vec_chunks"(chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,size INTEGER NOT NULL,validity BLOB NOT NULL,rowids BLOB NOT NULL)
```

</details>

### tag_vec_info

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| key | TEXT | NO | — | PK |
| value | ANY | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "tag_vec_info" (key text primary key, value any)
```

</details>

### tag_vec_rowids

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| rowid | INTEGER | NO | — | PK |
| id | — | YES | — | — |
| chunk_id | INTEGER | YES | — | — |
| chunk_offset | INTEGER | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "tag_vec_rowids"(rowid INTEGER PRIMARY KEY AUTOINCREMENT,id,chunk_id INTEGER,chunk_offset INTEGER)
```

</details>

### tag_vec_vector_chunks00

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| rowid | — | NO | — | PK |
| vectors | BLOB | NO | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "tag_vec_vector_chunks00"(rowid PRIMARY KEY,vectors BLOB NOT NULL)
```

</details>

### tags

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| id | INTEGER | NO | — | PK |
| namespace | TEXT | NO | `''` | — |
| name | TEXT | NO | — | — |
| notes | TEXT | YES | — | — |
| description | TEXT | YES | `NULL` | — |
| created_at | TIMESTAMP | NO | `CURRENT_TIMESTAMP` | — |
| canonical_id | INTEGER | YES | — | — |
| archived_at | TIMESTAMP | YES | `NULL` | — |
| archived_reason | TEXT | YES | `NULL` | — |
| last_injected_at | TIMESTAMP | YES | `NULL` | — |

インデックス:
- `idx_tags_archived_at` ON `tags`(archived_at)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "tags" (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  notes TEXT,
  description TEXT DEFAULT NULL
    CHECK(description IS NULL OR LENGTH(description) <= 100),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  canonical_id INTEGER REFERENCES tags(id), archived_at TIMESTAMP DEFAULT NULL, archived_reason TEXT DEFAULT NULL
  CHECK(archived_reason IS NULL OR LENGTH(archived_reason) <= 100), last_injected_at TIMESTAMP DEFAULT NULL,
  UNIQUE(namespace, name)
)
```

</details>

### topic_tags

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| topic_id | INTEGER | NO | — | PK |
| tag_id | INTEGER | NO | — | PK |

インデックス:
- `idx_topic_tags_tag` ON `topic_tags`(tag_id)

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE topic_tags (
  topic_id INTEGER NOT NULL REFERENCES discussion_topics(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (topic_id, tag_id)
)
```

</details>

### topic_vec

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| rowid | — | YES | — | — |
| embedding | — | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE VIRTUAL TABLE topic_vec USING vec0(
  embedding float[384] distance_metric=cosine
)
```

</details>

### topic_vec_chunks

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| chunk_id | INTEGER | NO | — | PK |
| size | INTEGER | NO | — | — |
| validity | BLOB | NO | — | — |
| rowids | BLOB | NO | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "topic_vec_chunks"(chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,size INTEGER NOT NULL,validity BLOB NOT NULL,rowids BLOB NOT NULL)
```

</details>

### topic_vec_info

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| key | TEXT | NO | — | PK |
| value | ANY | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "topic_vec_info" (key text primary key, value any)
```

</details>

### topic_vec_rowids

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| rowid | INTEGER | NO | — | PK |
| id | — | YES | — | — |
| chunk_id | INTEGER | YES | — | — |
| chunk_offset | INTEGER | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "topic_vec_rowids"(rowid INTEGER PRIMARY KEY AUTOINCREMENT,id,chunk_id INTEGER,chunk_offset INTEGER)
```

</details>

### topic_vec_vector_chunks00

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| rowid | — | NO | — | PK |
| vectors | BLOB | NO | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "topic_vec_vector_chunks00"(rowid PRIMARY KEY,vectors BLOB NOT NULL)
```

</details>

### vec_index

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| rowid | — | YES | — | — |
| embedding | — | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE VIRTUAL TABLE vec_index USING vec0(
  embedding float[384]
)
```

</details>

### vec_index_chunks

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| chunk_id | INTEGER | NO | — | PK |
| size | INTEGER | NO | — | — |
| validity | BLOB | NO | — | — |
| rowids | BLOB | NO | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "vec_index_chunks"(chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,size INTEGER NOT NULL,validity BLOB NOT NULL,rowids BLOB NOT NULL)
```

</details>

### vec_index_info

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| key | TEXT | NO | — | PK |
| value | ANY | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "vec_index_info" (key text primary key, value any)
```

</details>

### vec_index_rowids

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| rowid | INTEGER | NO | — | PK |
| id | — | YES | — | — |
| chunk_id | INTEGER | YES | — | — |
| chunk_offset | INTEGER | YES | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "vec_index_rowids"(rowid INTEGER PRIMARY KEY AUTOINCREMENT,id,chunk_id INTEGER,chunk_offset INTEGER)
```

</details>

### vec_index_vector_chunks00

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| rowid | — | NO | — | PK |
| vectors | BLOB | NO | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "vec_index_vector_chunks00"(rowid PRIMARY KEY,vectors BLOB NOT NULL)
```

</details>

### yoyo_lock

| カラム名 | 型 | NULL | デフォルト | PK |
|---|---|---|---|---|
| locked | INT | NO | `1` | PK |
| ctime | TIMESTAMP | YES | — | — |
| pid | INT | NO | — | — |

インデックス: なし（自動生成される主キー索引を除く）

<details><summary>CREATE文（生成元migration）</summary>

```sql
CREATE TABLE "yoyo_lock" ("locked" INT DEFAULT 1, "ctime" TIMESTAMP,"pid" INT NOT NULL,PRIMARY KEY ("locked"))
```

</details>
