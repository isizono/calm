---
name: tag-notes
description: タグのnotesを確認・更新する。「/tag-notes」「タグノート見せて」「このタグのnotes更新して」「〜のtag notesに追記して」など、特定タグに紐づく常備情報の閲覧・編集の意図で発動する。保存先が未確定の「覚えて」系依頼はrememberが担当するため発動しない。
---

# tag-notes

指定されたタグの notes を確認・更新してください。

## 手順

1. 引数でタグ名と内容が指定されていればそのまま使う
2. 指定されていなければユーザーに対象のタグと内容を確認する
3. `search_tags` で対象タグを検索し、現在の notes を取得する（対象タグ名をqueryに、include_notes=Trueで呼ぶ）
4. 既存の notes がある場合はその内容を保持しつつ、新しい内容をマージしたドラフトを作成する

   recompose/direction/backlog-review系のhint抑制マーカー（`#recompose-skipped`,
   `#recompose-bootstrap-skipped`, `#recompose-delta-skipped`, `#logs-sparse-ack`,
   `#direction-overflow-ack`, `#backlog-review-ack`）を追記・更新する場合の書式は以下の通り:
   - マーカー単体（例: `#recompose-skipped`）は恒久抑制
   - `<マーカー>-until:YYYY-MM-DD`（例: `#recompose-skipped-until:2026-08-01`）を付けると、
     指定日当日まで有効な期限付き抑制になる
   - 不正な日付形式は無視され、抑制は効かない（フェイルオープン。抑制しない側に倒す）
5. ドラフトをユーザーに提示し、確認を得る
6. `update_tag(tag=..., notes=...)` で書き込む（上書き方式のため全文を指定する）
