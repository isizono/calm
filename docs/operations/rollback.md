# cc-memory Revert / Rollback 手順書 v1

本ドキュメントは PR を取り消す（revert する）ときの手順を、R1/R2 の 2 分類ごとに 1 ページでまとめる。PR テンプレの「Revert」セクションはこの文書へのポインタとして使う想定。

内部 ID（D#/M#/A#/L#/T#）は本文に出さず論理名で書く。一次情報はコードであり、本ドキュメントと食い違った場合はコードが正である。

---

## 分類

- **R1（機械 revert 可）**: migration に触れていない PR。`git revert <merge commit>` のみで完結する
- **R2（データ手順つき）**: migration に触れている PR。`git revert` だけでは新しいテーブル・カラムが残る、あるいは古いテーブル・カラムが消えたままになる。データを戻す追加手順が要る

自分の PR がどちらに当たるかは、変更ファイルに `migrations/` 配下が含まれるかで判定する。

---

## R1: 機械 revert 可

```bash
git revert <merge commit>
```

コード側だけの変更であれば、これで終わる。念のため revert 後にテストスイートを流す。

---

## R2: データ手順つき

1. `git revert <merge commit>` でコードを戻す
2. データを戻す必要があるかを判断する:
   - migration が **追加のみ**（新規テーブル・新規カラムの追加で、既存データへの破壊的操作を伴わない）の場合、コードの revert だけで実害が無いことが多い（新しいテーブル・カラムは単に使われなくなるだけ）。ただし新カラムが `NOT NULL` かつデフォルト値なしで既存行に影響する場合は要検討
   - migration が **破壊的**（`DROP COLUMN` / `DROP TABLE` / データの `UPDATE` や `DELETE` を伴うテーブル再構築等）を含む場合、`git revert` だけではデータが戻らない。片道変換であり、以下のいずれかが必要:
     - **スナップショットからの復元**（唯一の確実な戻し方。ただし DROP 以降に行われた新規データの追加・変更も一緒に失われる）
     - **逆方向 migration の追加**（失われたデータが再構築可能な場合のみ有効。`DROP COLUMN` で失った値そのものは通常は戻せない）

### premigration スナップショットの探し方

現状、**migration 実行に連動した自動スナップショットは無い**（設計はあるが本書執筆時点で未実装。migration は SessionStart とは無関係にサーバー起動時に走るため、「migration 直前の状態」が保存されている保証はない）。

使えるのは SessionStart hook が定期取得するスナップショットのみ:

```bash
ls -la ~/.claude/.claude-code-memory/snapshots/
```

デフォルトは 12 時間間隔・最大 5 世代保持（保持ウィンドウ概算 2.5 日）。`.json` の `created_at` で、目的の migration が適用される前のタイムスタンプのものを選ぶ。**該当するスナップショットが無い場合（migration 適用から 12 時間以上経ってしまった等）、その時点のデータへは戻せない。**

このため、破壊的な migration を含む PR を適用する前は、以下を手動で実行してスナップショットを追加取得しておくことを推奨する（現状 CLI サブコマンドが無いため Python から直接呼ぶ）:

```bash
uv run python -c "from scripts.snapshot import take_snapshot; from src.db import get_db_path; print(take_snapshot(get_db_path()))"
```

### 復元コマンド

```bash
python scripts/snapshot.py restore <snapshot_db_path>
```

`sqlite3.backup()` で現行 DB に書き戻す。**不可逆**（復元前の状態を退避する仕組みは無い）ため、復元前に現状態も念のため取得しておくとよい。詳細は `docs/operations/debugging.md` §4 を参照。

### 復元後のサーバー再起動手順

1. 全ての Claude Code セッションを閉じる
2. 復元コマンドを実行する（上記）
3. HTTP サーバーを再起動する: `lsof -ti :52837 | xargs kill` の後、`uv run python -m src.launcher`（または既存の起動手順）
4. Claude Code セッションを再起動する

コードの revert とプラグインキャッシュの整合を取る必要がある場合（PR マージ直後の revert 等）は、`CLAUDE.md` の「PR マージ後の反映手順」に従ってプラグインキャッシュも合わせて更新する。

---

## 補足: 現状の制約

- R1/R2 の自己申告と `migrations_touched` の機械突き合わせ（矛盾時に CI コメントで指摘する仕組み）は別途進行中で本書執筆時点では未実装。現状は本ドキュメントの分類基準を目視で当てはめる
- スナップショットの kind 分離（定期取得と migration 前取得を別クォータで管理する等）も別途進行中で未実装。上記の「手動取得」はその代替手段
