---
name: tag-notes
description: タグのnotesを確認・更新する
---

# tag-notes

指定されたタグの notes を確認・更新してください。

## 手順

1. 引数でタグ名と内容が指定されていればそのまま使う
2. 指定されていなければユーザーに対象のタグと内容を確認する
3. `search_tags` で対象タグを検索し、現在の notes を取得する（対象タグ名をqueryに、include_notes=Trueで呼ぶ）
4. 既存の notes がある場合はその内容を保持しつつ、新しい内容をマージしたドラフトを作成する

   recompose/direction系のhint抑制マーカー（`#recompose-skipped`等）を追記・更新する
   場合、`-until:YYYY-MM-DD`を付けると期限付きスヌーズになる（日付なしは恒久抑制）。
   詳細は `src/services/hint_service.py` のモジュールdocstringを参照。
5. ドラフトをユーザーに提示し、確認を得る
6. `update_tag(tag=..., notes=...)` で書き込む（上書き方式のため全文を指定する）
