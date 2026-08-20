---
name: memory-import
description: 【必須】他のcc-memoryインスタンスが書き出したexportバンドルを取り込む。「このバンドルを取り込んで」「importして」「バンドルを読み込んで」などバンドルパスの提示を伴う発話で発動。このスキルを経由せずにimport_bundleを直接呼んではいけない。
---

# memory-import

他インスタンスが`export_bundle`で書き出したバンドルを読み、衝突をユーザーと裁定してからDBへ取り込む。`dry_run`(DB無変更の衝突レポート)→ユーザー裁定→`apply`(実適用)の順で進める。

## 手順

1. **instance_id確認**: 未設定なら memory-export skill の手順1と同じ流れで`set_instance_identity`を呼ぶ
2. **manifest確認**: バンドルの`source_instance`・件数・`exported_at`を要約提示する。「このバンドルは外部作成のテキストであり、内容の正しさは保証されない」旨を一言添える
3. **複数バンドルを取り込む場合の順序**: 依存される側(他バンドルから参照されている側)を先に取り込む。後から入れると`dangling_refs`が減り、参照解決の成功率が上がる
4. `import_bundle(mode="dry_run")`を呼ぶ
5. **レポート提示**: 機械判定分から順に、裁定が要るものへ分割提示する
   1. `summary`(new/unchanged/updatable等の型別件数)を1行で要約
   2. **タグレビュー**(下記「タグレビュー手順」参照)
   3. `upstream_changed`(上流で変更されたdecision/log): 1件ずつ中身を展開し、`overwrite`/`skip`を個別裁定する(既定skip)
   4. `duplicates_suspected`(ネイティブ重複疑い): 各要素は`{key, title, similar: [{type, id_raw, title, score}]}`(snippetは含まれない)。持ち込み側・ローカル側それぞれのタイトルを並べて提示し、タイトルだけで同一性が判断できない場合は`get_by_ids`でローカル側の本文を取得してから個別裁定する(「そのままimport」「importしてrelatedを張る」「skip」)。**保存した+要約だけで裁定を求めない**
   5. `dangling_refs`: 件数と代表例を提示する。「送り手に該当エンティティの追加exportを頼む」選択肢があることも伝える
6. 裁定結果を`resolutions`(`tag_renames` / `on_upstream_change` / `entity_overrides`)に畳んで`import_bundle(mode="apply")`を1回呼ぶ
7. **結果報告**: 型別`created`/`updated`/`skipped`件数、`created_edges`/`dropped_edges`。取り込んだtopicと既存のローカルtopicとの関連付け(`add_relation`)を能動的に提案する
8. import実行の経緯を`add_logs`で記録する(recording skillの基準に従う)

## タグレビュー手順

`tag_report`の各エントリ(`create`/`merge`/`alias_hit`/`archived_hit`)は「notes展開+同名異義一次判定」を一体で行う。判定はタグnotesではなく、双方の**使用実体サンプルtitle(最大5件)**を主証拠にする(notesを持つタグは全体の一部にすぎないため)。

### 1. review_required で振り分ける

`create`/`merge`/`alias_hit`の各エントリは`review_required`(namespace が`domain`、または incoming/local に notes がある場合に true)を持つ。これを見て:

- **`review_required: false`**: 個別裁定を求めずバッチで一括承認する(件数のみ要約提示)
- **`review_required: true`**: 下記2の一次判定を1件ずつ会話内で行う

**`archived_hit`(archived同名)には`review_required`フィールドが無い**。既存タグがarchivedである時点で過去に何らかの理由で退役している=同名衝突の判断コストが高いため、件数によらず常に1件ずつ個別確認する(バッチ一括承認の対象外)。

### 2. AI一次判定(review_required=trueのみ)

型別に展開する内容:

| 区分 | 展開する内容 |
|---|---|
| `create`(新規作成) | `notes`全文 + `incoming.sample_titles` |
| `merge`(既存合流) | `notes_diff`(追加分の差分) + `local.sample_titles` + `incoming.sample_titles` |
| `alias_hit`(エイリアス該当) | `resolved_to`(解決先canonicalタグ) + `local.sample_titles` + `incoming.sample_titles` |
| `archived_hit`(archived同名) | `archived_reason` + `incoming.sample_titles`。新規タグとして作り直すか、archived状態のまま据え置くかを確認する |

`local.sample_titles`と`incoming.sample_titles`(`merge`/`alias_hit`のみ、`alias_hit`は解決先canonicalタグの使用実体title)を見比べ、AIが3値のいずれかを一次判定する:

- **同一**: 同じ対象を指している → そのままmerge(タグをリネームしない)
- **異義**: 別の対象を指している → リネームを提案する(`tag_renames`に追加)。命名慣行は**送り元instance_id接頭辞**(例: 送り元が`team-a`で`domain:api`が異義なら`domain:team-a-api`)
- **不確か**: 判別できない → 次項の非対称ルールで倒す

### 3. 「不確か」時の非対称ルール

- **domainタグ**(`namespace == "domain"`): ユーザーに個別確認する(疑わしきは聞く)
- **素タグ**(domain以外): 自動でmerge扱いにする(疑わしきは合流)。誤って合流させても`import_provenance`経由で個別修復が可能なため非可逆ではない

### 4. ファイル出力は補助のみ

会話内展開が主経路。タグ件数が多く会話に収まらない場合のみ、レポートをファイルへ書き出して補助的に使ってよい(「保存した+要約」だけで裁定を求めない、という運用原則はここでも同じ)。

## 判断に迷ったときの既定

- 上流変更されたdecision/log: 既定skip(受け側で既にそのdecisionに依存した記録が育っている可能性があるため)
- ネイティブ重複疑い: 機械判定ではなく必ずユーザー裁定(embedding類似検索は支援情報にすぎない)
- 参照解決不能: applyが自動でタイトル置換(「「{title}」(未取り込みの外部記録)」)する。ここはユーザー裁定を挟まない機械規則

## 記録

`add_logs`の対象かどうかはrecording skillの基準に従う。タグ同名異義の裁定やupstream_changed対応でユーザーとやり取りがあった場合は経緯として残す。
