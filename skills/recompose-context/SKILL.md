---
name: recompose-context
description: アクティビティ・トピック・decisionなどの全関連情報を統合・整理し、anchor対応表を作って次のcheck-inを最適化する。リコンサイル（無効化・再編）も同時に行う。「/recompose」「recompose」「情報整理して」「まとめて」「リコンサイル」などで発動。
---

# recompose-context

指定された入口（activity/topic/decisionどれでもOK）から関連する全情報（topics, decisions, logs, materials）を読み込み、統合material（anchor対応表付き）の生成・軽照合・リコンサイル・tag pin・tag-notes更新を行う。**次のcheck-inだけで作業に必要な情報が全部揃っている状態**を作ることがゴール。

anchorの新規作成・更新は [setup-anchor](../setup-anchor/SKILL.md) skillに委譲する（責務分離）。

## 整理単位

整理単位は「2-3個のtopicを囲む形で増殖した関連A/D群」。入口がactivity/decisionでも、関連relationを辿ってtopicまで遡って整理する。

## 発動契機

実行は手動（ユーザーが「やるか」と言って初めて走る）。check-in時のナッジhint（tagスコープ内のdecision増分検知）から誘導されることもある。実行コンテキストは限定しない: 別セッションで単独実行しても、sync-memoryの延長でやってもよい。

## 手順

### 1. 入口の特定とスコープ提示

引数で entity_type + entity_id が指定されていればそのまま使う。指定されていなければ、文脈（check-in済みactivity・直近の話題・ナッジhintの対象tag）からエージェントが自律判断で決める。activity/topic/decisionどれでも可。

エージェントがスコープを切ったら、**実行前にユーザーへ一行で規模感を提示してOK確認を取る**。背景: cc-memoryの記録内容はユーザー自身も把握していない前提なので、スコープの中身を見せる代わりに作業規模だけ示す。

提示フォーマット例:
> 「ざっと N activities + M decisions ぶん、SA X 人で並列整理する。これで進める？」
> 「整理単位が大きいので 2 cluster に分けて並走させる。SA Y 人。これで進める？」

提示する内容:
- 関連エンティティの概数（activities + decisions + materials の合計件数感）
- 並列起動する SA の人数（cluster 数で決まる、目安: 各 SA が 1 cluster を担当）
- 分割した場合はその数を明示

提示しないこと:
- どの activity/decision がどの cluster に入るかの内訳（ユーザーは記録を把握していない）
- 関連エンティティの個別 ID 一覧

OK が出たら自走モードに入り、以降ステップ2-8はユーザーへの追加確認なしで進める。OK が出るまでは記録 read のみで write はしない。

判断材料が本当にないとき（入口候補がゼロ）だけ「どこから整理する？」を聞く。

### 2. 全情報の収集

入口から関連エンティティを総ざらいする:

1. 入口に応じた取得: activityなら `check_in`、topicなら `get_decisions`/`get_logs`、decisionなら `get_by_ids` などで起点情報取得
2. `get_map(entity_type, entity_id, max_depth=3)` で関連エンティティを辿る（depthが足りなければ運用で上げる）
3. `search` で言及されているが正式relationが張られていないentityを補完探索
4. 各エンティティのget系で全文取得（ページネーション、retract済みは除外）

**フィルタ方針**: retract済みdecision/logのみ除外。completedなactivityも収集対象に含める（間引き対象として扱うため）。

### 3. 統合materialの生成（anchor対応表付き）

#### サイズ予算

2,000〜3,000字を目安、最大5,000字まで許容（参考値、実運用で調整）。このmaterialはcheck-inのたびに**全文注入**されるため、予算超過は継続コストになる。超えそうなら要約の密度を上げるか、整理単位の分割を検討する。

#### tagスコープとの対応

tagスコープとmaterialは原則1対1。1つのtagに複数のmaterialが必要になった場合は、materialを分けるのではなく1 material内にインデックスセクションを作り、深掘りは `search` / `get_material` へ誘導する（更新責務を曖昧にしないため）。

#### セクション構成

```
## 現状
（どこまで進んでいるか、今どのフェーズか。経緯の概観を含む）

## 合意事項
（有効なdecisionsの要約。各項目はWHAT + WHYで書く。
  supersedes済みは除外。[議論中]タグのものは「残論点」に分類する。
  次のエージェントがget_decisionsしに行かなくても判断の背景を
  理解できる粒度で書く）

## anchor対応表
| 合意事項参照 | anchor型 | 検証先 |
|---|---|---|
| D#xxxx の要約 | 実装済 | src/services/foo.py:bar |
| D#yyyy の要約 | 未実装 | bar.py周辺 + D#zzzz の前後関係 |

## 注意事項
（検討して却下された代替案、議論中の制約条件、暗黙の関連 など）

## 残論点
（未解決の議論、次にやるべきこと、軽照合で見つかったズレ）
```

#### 3-a. anchor自動推測

各合意事項について、エージェントが anchor型と検証先を推測する。型と書き方:

| 型 | 検証先 |
|---|---|
| 実装済 | 該当コードのファイル+関数 |
| 外部仕様準拠 | doc URL |
| 未実装 | 周辺の実装済みコード＋関連decisionの前後関係 |
| 事実調査 | material内の一次ソース |

「最新decisionを静的参照」は採用しない。

推測できた合意事項は anchor対応表 に直接追加。推測できない / 確証が低いものは **buffer** に溜める。

#### 3-b. setup-anchor 起動

buffer が空でなければ setup-anchor skill を起動して、ユーザーと対話してanchor確定する。結果（mode=created）は anchor対応表 に merge する。

#### 3-c. anchor対応表の統合

完成した anchor対応表 を material.content の「## anchor対応表」セクションに書き込む。

### 4. 軽照合

各合意事項について、anchorに対してSAに指示出ししてズレを確認する:

- 実装済 → 「このコード（パス）が、この合意（テキスト）と整合しているか確認して」
- 外部仕様 → 「このdoc（URL）が、この合意の根拠として通用するか確認して」
- 未実装 → 「周辺コード＋関連decisionの前後関係から、この合意の方向で進めて問題ないか確認して」
- 事実調査 → 「このmaterialの一次ソースが、この合意の根拠として通用するか確認して」

SA（run_in_background）で並行実行。結果はマトリクス化して、合意との一致/ズレを記録。

- 一致 → そのまま維持
- ズレ → 「残論点」に追加。ズレが「anchor側が古い」起因なら setup-anchor の更新モードに回す候補としてマーク

### 5. リコンサイル（無効化・再編）

軽照合や情報棚卸しで「無効化・再編すべき」と判断したエンティティを、自律度ルールに従って処理する。

#### 無効化・再編アクション（種別別）

- decision/log → `retract`
- material → `update_material` で上書き
- topic/activity → retract非対応のため「直す」: status調整・合流・relation張り・新規作成

#### 自律度ルール（暫定・運用後見直し前提）

| ゾーン | アクション例 |
|---|---|
| 🟢 自律 | relation張り / status・title調整 / material上書き |
| 🟡 確証あれば自律 | decision/log retract / activity合流 |
| 🔴 確認 | 新規entity作成（material以外）|

#### 怪しき発火条件（全レーン共通で🔴に格上げ）

以下のいずれかに該当する場合は確認レーンに格上げする:

- 矛盾で新旧不明
- [議論中]タグ
- log に懸念明記
- 推測でしか判定不可

確認はバッファに溜めて最後に一括提示・ジャッジ。重大な矛盾だけはその場で確認（ハイブリッド）。

### 6. material の保存と tag pin

1. 整理単位の代表tagを1つ選ぶ（例: 対象topicのdomainタグ、`recompose-context`, `anchor`, `reconciliation` などその整理単位を表すtag）
2. 既存material（同じtag + 対象エンティティへのrelation を持つ）を探す
3. **既存あり**: 上書き前のcontentを `add_logs` で退避してから（履歴保全、バージョニング機構の代わり）、`update_material` でcontent/title/tags/sourceを上書き
4. **既存なし**: `add_material` で新規生成
    - タイトル: `recompose: {対象エンティティのtitle}（YYYY-MM-DD）`
    - tags: 代表tag + `recompose-context` を含める
    - `related` で対象エンティティに紐づける
5. `add_pin(source_type='tag', source_ref=<代表tag>, target_type='material', target_ref=<material_id>)` で代表tagにmaterialをpinする
6. 複数ヒットした場合は警告を出してabort（正常系は1件のみ）

### 7. tag-notes更新

対象entityに紐づく全タグについて、以下を自問する:

- **追加型**: 既存tag-noteに書かれていない横断知見はないか？同じタグを持つ他entityにも適用される教訓・運用ルールはないか？
- **改訂型**: 既存tag-noteと矛盾する新しい合意はないか？→書き換え / 前提が覆った記述はないか？→削除or書き換え

**書かないもの**: 一過性の話題 / entity固有の文脈（→material行き）/ 既存記述と重複する内容

判定が出たタグは `update_tag` で上書きする。肥大化したtag-noteは縮約する（判断任せ）。1タグ失敗でも他タグ処理は継続。該当なければスキップ。

### 8. relation補完

logs・decisionsの中で言及されているが正式relationが張られていないtopic/activity/materialを発見したら `add_relation` で補完する。

該当なければスキップ。

### 9. ユーザーへの報告

```
## recompose-context 完了: {対象エンティティのtitle}

### 統合material
- M#xxx（tag pin: <代表tag>）

### anchor対応表
- 合計N件（推測のみ N件 / setup-anchor確定 N件 / 更新 N件）

### 軽照合
- 一致 N件 / ズレ N件（→残論点）

### リコンサイル
- 自律実行: N件
- 確認待ち: N件（バッファ提示）

### tag-notes更新
- （更新したタグ。なければ「なし」）

### relation補完
- （追加したrelation。なければ「なし」）
```

確認待ちがあれば、報告後にユーザーと一括ジャッジする。

## 注意

- **鵜呑み防止ガード**: 確証なく判断できないもの（矛盾で新旧不明 / [議論中] / log懸念 / 推測でしか判定不可）は自律retractせず確認レーンへ。重大な矛盾は手動運用ならその場で確認
- **anchor推測の枠**: anchorは「正解を当てる」のではなく「判断ができる場所を指す」だけで十分。「最新decisionを静的参照」は採用しない
- 情報を要約しすぎない。check-inだけで完結する密度が必要
- ステップ2〜8ではユーザーへのテキスト出力を最小限にする。最終報告（ステップ9）でまとめて伝える
- 自律度ルールは暫定。一度運用してから見直す前提
