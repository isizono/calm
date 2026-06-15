---
name: setup-anchor
description: 合意事項のanchor（検証先＝どこを見れば正しさを判断できるか）をユーザーと対話して新規確定または更新する。recompose-contextから呼ばれるほか単独でも利用可能。「/setup-anchor」「anchor作って」「アンカー設定」「検証先決めたい」「anchor更新したい」などで発動。
---

# setup-anchor

合意事項（decision/log/material）の anchor（「この合意が正しいか、どこを見れば判断できるか」を表すテキスト）を、ユーザーと対話して**新規確定 or 既存更新**する。

## 役割の境界

- ✅ anchor**新規**作成（recompose側で推測不能だった合意事項に対して）
- ✅ 既存anchorの**更新**（コードパスが変わった / URL変わった / 関連decisionが入れ替わった などのズレ解消）
- ❌ anchorを使った合意の検証（recompose側の軽照合SAの責務）

## anchorの型

| 型 | 検証先の書き方 |
|---|---|
| 実装済 | 該当コードのファイル+関数（例: `src/services/checkin_service.py:check_in`）|
| 外部仕様準拠 | doc URL |
| 未実装 | 周辺の実装済みコード＋関連decisionの前後関係（例: 「`pin_service.py:add_pin` の整合を見る」）|
| 事実調査 | material内の一次ソース（例: `materialの調査結果`）|

「最新decisionを静的参照」は採用しない。未実装合意は周辺コード見渡しが基本。

## 入出力

### 入力
合意事項のリスト。各要素:

- `entity_type`: `decision` / `log` / `material`
- `entity_id`: int
- `text`: 合意事項のテキスト
- `anchor_candidate`: (optional) recompose側で推測した候補
- `existing_anchor`: (optional) 更新対象の既存anchor（型 + verification_target）

`existing_anchor`の有無で **新規モード / 更新モード** を判別する。単独利用時は入力フォーマットを満たさない自然言語入力でもよい（ユーザーから対象合意事項を聞き出す）。

### 出力
anchor対応表エントリのリスト。各要素:

- `entity_type`, `entity_id`: 入力と同じ
- `anchor_type`: 実装済 / 外部仕様 / 未実装 / 事実調査
- `verification_target`: 具体的な検証先テキスト
- `mode`: `created` / `updated`（呼び出し元が差分を扱えるように）

### 副作用
なし。DBへの書き込みは行わない。recompose側がmaterial pinとして永続化、単独利用時は呼び出し元エージェント or ユーザーが扱う。

## 手順

### 1. 入力の確認
入力リストの件数と、新規 / 更新の内訳をユーザーに提示する。

### 2. 各合意事項を順に処理

1. 合意事項テキストを表示。更新モードなら既存anchorも併せて表示
2. anchor候補の提示:
    - 入力に `anchor_candidate` があれば提示
    - なければユーザーに直接聞く（エージェントの推測を強制しない）
    - 余裕があればエージェントが軽く探して候補を出してから聞くのもOK（裁量）
3. anchor型をユーザーと合意（4型から選択）
4. 検証先を具体化:
    - 実装済 → コードパスを `grep`/`find` で実在確認してから記録
    - 外部仕様 → URLは記録のみ（生きてるかは確認しない）
    - 未実装 → 周辺コード＋関連decision IDをセットにして記録
    - 事実調査 → material IDを指定して記録
5. 更新モードの場合は「既存→新」の差分を提示してから承認
6. ユーザー承認を得てから次へ

### 3. 結果のまとめ
確定したanchor対応表（mode付き）をユーザーに提示し、呼び出し元に返す形にまとめる。

## 注意

- anchor推測は「正解を当てる」ではない。**判断ができる場所を指す**だけで十分
- 1件失敗でも他の処理は継続する。エラーは記録して最終報告に含める
