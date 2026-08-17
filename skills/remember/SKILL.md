---
name: remember
description: 【必須】ユーザーが「覚えて」「保存して」「これ残して」「忘れないで」「メモって」「記録して」など、何かを記憶・永続化することを依頼したときに発動。情報の保存先を判定するフローを提供する。このスキルを経由せずにauto-memory・CLAUDE.md・habits・tag-notes・rulesに直接書いてはいけない。TRIGGER: ユーザーが何かを覚えておくこと・保存すること・記憶に残すことを依頼した時、表現のバリエーションは問わない。DO NOT TRIGGER: CALMのadd_decisions/add_logsなど議論記録としての保存指示、過去の記録が現状と矛盾・陳腐化しているという訂正・撤回の依頼（forget skillの担当）。
---

# remember

【必須】ユーザーが記憶を依頼した。以下のフローで保存先を判定し、保存先をユーザーに伝えてから実行する。新規保存専用のスキルであり、過去の記録の訂正・撤回は [forget](../forget/SKILL.md) skillの担当。

**重要: 必ずこのフローで保存先を判定してからユーザーに確認し、実行する。**

## 判定フロー

```
そのルール/情報は…

├─ 過去の記録が現状と矛盾・陳腐化しており、訂正・撤回が必要？
│   └─ YES → このスキルの対象外。forget skillへ
│
├─ 特定のファイルに触れる時だけ必要？
│   └─ YES → ~/.claude/rules/（パス付き）
│       （`cc-memory-habits.md`はhabits DBからの自動投影ファイルで手動編集対象外。
│         対象はそれ以外の手動管理rulesファイル）
│
├─ 特定のタグ文脈でだけ必要？
│   └─ YES → tag-notes（update_tagで記録）
│
├─ どのプロジェクト・文脈でも常に適用される？
│   ├─ 「従え」系の制約・ルール？
│   │   └─ YES → habits（add_habitで記録）
│   └─ 「知っとけ」系の文脈・tips？
│       ├─ 重要度高い → ~/.claude/CLAUDE.md
│       └─ 重要度低い → auto-memory
│           （同じ趣旨のfeedbackを繰り返し書いている場合は、横断ルールとして
│            ~/.claude/rules/への格上げをユーザーに提案する）
```

## 判定の2軸

- **スコープ**: グローバル（全プロジェクト共通） / タグスコープ（特定の文脈でのみ） / ファイルスコープ（特定のファイルパターンに触れる時のみ）
- **性質**: 制約（「従え」系） / 文脈（「知っとけ」系）

## habits投影の仕組み

`add_habit`で記録したhabitは常にtrigger_mode='intelligently'（マニフェスト表示）で作成される。SessionStart時に~/.claude/rules/へ自動投影されるが、intelligently層はタイトルのみで、全文は`get_habits`でのon-demand取得が必要になる。

「毎回必ず全文で見てほしい」レベルの制約は、記録後に`update_habit`で`trigger_mode="always"`へ昇格する追加ステップが要る（新規作成と同時にalways指定はできない）。判定に迷う場合はユーザーに確認する。

## 注入の強度

```
強 ┃ habits（always層）        — 全文をSessionStartで常時投影。絶対守れ
   ┃ tag-notes                 — この文脈に触れるなら絶対必要
   ┃ rules/                    — このファイルに触れるなら従え
   ┃ CLAUDE.md                 — プロジェクト文脈として参照
   ┃ auto-memory               — 見えてはいるけど、気づけば使え程度
   ┃ habits（intelligently層） — タイトルのみ常時投影、全文はget_habitsで取得
弱 ┃
```

## 注意

- 判定に迷ったらユーザーに確認する
- CALMのdecisionは「覚えて」の対象外（双方合意の事実記録用）
- auto-memoryとCALMの二重記録はOK（異なる注入経路として機能する）
- 過去の記録の訂正・撤回は対象外。矛盾・陳腐化に気づいたら[forget](../forget/SKILL.md) skillへ
