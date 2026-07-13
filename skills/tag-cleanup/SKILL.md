---
name: tag-cleanup
description: タグの共起分析を実行し、整理提案をユーザーに提示して承認後に実行する。「/tag-cleanup」「タグ整理して」「タグが散らかってきた」「似たタグをまとめて」「タグの棚卸し」などで発動する。個別タグのnotes編集（tag-notes）には発動しない。
---

# tag-cleanup

`analyze_tags` でタグの共起分析を実行し、整理提案をユーザーに提示する。ユーザーが承認したアクションのみ実行する。

## 手順

1. 引数で `domain` が指定されていればそのまま使う。指定されていなければ、どのドメインで分析するか（または全体か）をユーザーに確認してから `analyze_tags()` を呼ぶ
2. 結果を以下の4セクション構成でユーザーに提示する
3. ユーザーが承認したアクションを `update_tag` で実行する
4. 実行結果を `add_material` で保存する

## 提示フォーマット

分析結果を以下の順序で提示する。該当がないセクションは省略する。タグ名のみで提示し、内部IDは表示しない。

### 1. 重複候補（suspected_duplicates）

名前が類似しているタグのグループ。rename または canonical 化を提案する。

```
## 重複候補
| グループ | タグ | 提案 |
|---------|------|------|
| 1 | `checkin`, `check-in`, `activity-checkin` | `check-in` に統一（他を canonical 化） |
```

提案の方針:
- usage が最も多いタグ、または最も一般的な命名規則に合致するタグを正規名にする
- 残りは canonical 化（エイリアスとして維持）または rename（完全置換）を提案

### 2. クラスタ（clusters）

自然にまとまるタグ群。まとめタグの新設や既存タグへの集約を提案する。

```
## クラスタ
| # | タグ群 | cohesion | 提案 |
|---|--------|----------|------|
| 1 | `stop-hook`, `hook-system`, `pretooluse`, `event-driven` | 0.85 | まとめタグ不要（既に `hook-system` がある） |
```

提案の方針:
- まとめタグがすでに存在する場合はその旨を記載
- 存在しない場合は「`xxx` タグの新設を検討？」と提案

### 3. 孤児タグ（orphans）

usage が低く、吸収先がありそうなタグ。canonical 化を提案する。

```
## 孤児タグ
| タグ | usage | 最寄り | PMI | 提案 |
|------|-------|--------|-----|------|
| `hook_state` | 1 | `hook-system` | 2.1 | `hook-system` に canonical 化 |
```

提案の方針:
- nearest と PMI を参考に吸収先があれば canonical 化を提案する
- 吸収先が無い（nearest が null）、または統合ではなくタグ自体を使わなくする場合は archived 化（退役）を提案する。archived は物理削除ではなく `archived=False` でいつでも解除できるため、判断に迷う孤児タグはまず archived を優先する

### 4. 共起情報（co_occurrences）

タグ間の関係の強さ。参考情報として提示するのみで、直接のアクション提案はしない。

```
## 共起情報（参考）
| タグA | タグB | PMI | 共起数 |
|-------|-------|-----|--------|
| `stop-hook` | `hook-system` | 3.2 | 30 |
```

## アクション実行

ユーザーが承認したアクションのみ実行する。自動実行はしない。

- **canonical 化**: `update_tag(tag="旧タグ", canonical="正規タグ")`
- **rename**: `update_tag(tag="旧タグ", rename="新タグ")`
- **archived 化（退役）**: `update_tag(tag="対象タグ", archived=True, archived_reason="理由")`。他タグの canonical 先になっているタグは archived 化できない（先にエイリアス解除が必要）
- 実行後、変更内容を一覧で報告する

## 結果の保存

実行したアクションの一覧を `add_material` でタグ付きで保存する。タイトルは「タグ整理結果 YYYY-MM-DD」とする。内容には実行したアクションと変更前後の状態を含める。タグには `domain:` タグと `tag-cleanup` を付ける。
