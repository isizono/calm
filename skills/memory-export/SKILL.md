---
name: memory-export
description: 【必須】cc-memoryの記録(topic/decision/log/material/activity)を他のcc-memoryインスタンスへexportバンドルとして書き出す。「エクスポートして」「他のインスタンスに渡したい」「バンドル作って」「知識を共有したい」などで発動。このスキルを経由せずにexport_bundleを直接呼んではいけない。
---

# memory-export

起点(topic/activity)またはタグから到達可能な記録を選び、他のcc-memoryインスタンスへ渡すバンドル(manifest.yaml + エンティティ別mdファイル)を書き出す。

## 手順

1. **instance_id確認**: `get_config`で`instance_id`を確認する。`null`なら未設定。DNSラベル風の命名規則(`^[a-z][a-z0-9-]{2,31}$`)を示してインスタンス名を1ターンで聞き、`set_instance_identity`を呼ぶ。**一度設定すると原則変更不可**(forceなしでは拒否される破壊的操作である旨を必ず伝える)
2. **起点の種類を確認**: 「特定のトピック/アクティビティから辿る」か「特定のdomainタグ配下をまとめて渡す」かをユーザーの発話から判定する(不明なら1ターン確認)
   - 前者: 起点テーマを`search`で探し、起点entity(topic/activity)をユーザーに確認する
   - 後者: 対象の`domain:`タグを確認する。複数domainにまたがる知識を渡す場合は、**tag_rootsを複数指定した1バンドルへ統合するのが正解**であり、バンドルを分けない(本文中の参照は解決不能だとタイトル置換で非可逆に劣化するため、分割よりバンドル統合の方が受け手側の文脈が保たれる)
3. `collect_export_candidates`を呼ぶ
   - 起点方式: `roots`(+`max_depth`、既定2)
   - タグ方式: `tag_roots`(グラフ拡張はしない、深度ゼロ固定でシード集合に合流)
4. **候補提示**: 総候補件数に応じて提示ポリシーを切り替える(下表)
5. **確定リストの復唱**: 型別件数と主要タイトルを要約提示し、最終確認を取る
6. `export_bundle`を呼ぶ。`selection`引数に手順3の入力(`roots`/`tag_roots`/`max_depth`)をそのまま渡す(将来の同一selectionでの再exportの前提になる)
7. **結果報告**: 書き出しパス・型別件数・`unresolved_refs`(受け手への申し送りになる旨。各要素に`domain_tags`が付くので「次にどのdomainのバンドルを追加で送ってもらうべきか」の判断材料になる旨を説明)・`masked_literals`件数
8. export実行の経緯を`add_logs`で記録する(recording skillの基準に従う)

## 候補提示ポリシー

| 状況 | 提示方法 |
|---|---|
| 候補が少数(目安: 総候補が数十件程度まで) | 型別にグルーピングして分割提示する(一度に全部出さない)。decision一覧→裁定→material一覧→…の順。ユーザーが「全部いい」と言ったら残りをまとめてよい |
| 候補が多数(目安: 総候補が数百件規模、`truncated: true`が返る規模を含む) | 個別裁定を求めない。「全量in既定+retracted/superseded既定out+opt-outの列挙だけ確認」というバッチ先行の提示に切り替える |

型別の見せ方(いずれの規模でも共通):

| 型 | 提示内容 |
|---|---|
| decision | title + retracted/supersededフラグ |
| topic / activity | title + snippet |
| material | title + snippet + サイズ(`size_chars`) |
| log | 既定`include_types`から除外済みのため候補にも出さない(export対象外が既定のルール) |

- `retracted`/`superseded`のcandidateはフラグ付きで表示し、既定は未選択のまま伝える
- `closure_warnings`があれば候補提示より先に見せる(「supersede先/引用先が選択範囲外」)
- タグ方式(`tag_roots`)で呼んだ場合、レスポンスの`co_tags`のうち`share`が高いタグを「一緒に含めますか」と1回だけ確認する(同梱するかはユーザー裁定、既定は含めない)
- activityは自動同梱の経路がない。候補一覧で明示的にチェックされた場合のみ選択リストに入る(親topicのような機械的な強制同梱はdecision/logのみの規則)

## 判断に迷ったときの既定

- decision/logのsupersede先が選択範囲外: 既定は非同梱(エッジ情報のみ)。`closure_warnings`で検知されたら1回だけ同梱するか確認する
- retractedエンティティ: 既定では未選択のまま候補に残す。明示選択されればそのままバンドルに含める

## 記録

`add_logs`の対象かどうかはrecording skillの基準(議論の経緯・詰まって解決した経緯等)に従う。単なる定型実行だけなら省略してよいが、選択方針でユーザーとやり取りがあった場合は経緯として残す。
