---
name: remember
description: 【必須】ユーザーが「覚えて」「保存して」「これ残して」「忘れないで」「メモって」「記録して」など、何かを記憶・永続化することを依頼したときに発動。情報の保存先を判定するフローを提供する。このスキルを経由せずにauto-memory・CLAUDE.md・habits・tag-notes・rules・pinに直接書いてはいけない。TRIGGER: ユーザーが何かを覚えておくこと・保存すること・記憶に残すことを依頼した時、表現のバリエーションは問わない。DO NOT TRIGGER: 議論の合意事実の記録（decision-record skillの担当）、経緯・成果物の記録（recording skillの担当）、過去の記録の訂正・撤回（forget skillの担当）、メタask裁定の発効に伴う配置（rule-placement skillの担当）。
---

# remember

【必須】ユーザーが記憶を依頼した。以下のフローで保存先を判定し、保存先をユーザーに伝えてから実行する。新規保存専用のスキルであり、過去の記録の訂正・撤回は [forget](../forget/SKILL.md) skillの担当。

この判定木は [rule-placement](../rule-placement/SKILL.md) skillの4軸評価（契機/型/リスク/改訂見込み）から導出された高速表である。日常の「覚えて」はこの判定木で足りる。判定木の末端で迷う場合、1つの依頼に性質の異なる複数ルールが混ざっている場合は、rule-placementのfull評価へ切り替える。本判定木を改訂するときは、rule-placement側の評価体系から導出できる形を保つこと（片側だけの改訂は禁止。導出関係が崩れていたら矛盾として扱う）。

**重要: 必ずこのフローで保存先を判定してからユーザーに確認し、実行する。**

## 判定フロー

```
そのルール/情報は…

├─ 過去の記録が現状と矛盾・陳腐化しており、訂正・撤回が必要？
│   └─ YES → このスキルの対象外。forget skillへ
│
├─ 「〜な状態なら〜してよい/すべき」という条件付き判断基準？
│   （適用条件と適用外を書き分けられ、隣接文脈に誤適用したら害がある型。
│     例外条項付きの禁止はこれではなく無条件規範）
│   └─ YES → 判例decision（add_decisionsで記録。reasonに適用条件:/適用外:の
│       定型節を含める。書式は docs/precedent-format.md）
│       （要る場面のタグやツールを名指しできるなら、そのtag-notes/docstring側
│         から判例への参照を添えることを検討する）
│
├─ 特定のファイルに触れる時だけ必要？
│   └─ YES → ~/.claude/rules/（パス付き）
│       （`cc-memory-habits.md`はhabits DBからの自動投影ファイルで手動編集対象外。
│         対象はそれ以外の手動管理rulesファイル）
│
├─ 特定のタグ文脈でだけ必要？
│   └─ YES → tag-notes（update_tagで記録）
│
├─ 特定のactivity/topicに取り組んでいる間だけ必要？
│   └─ YES → pin（内容をdecision/materialとして記録し、add_pinで対象へ
│       括り付ける。pin元へのcheck_inのたびに本文ごと再配達される）
│
├─ どのプロジェクト・文脈でも常に適用される？
│   ├─ 「従え」系の無条件規範？（条件節なしの1文で書ける）
│   │   ├─ 毎回必ず守るべき（違反の実害が大きい）
│   │   │   ├─ 100字未満で書ける → habits（add_habit後、update_habitで
│   │   │   │    trigger_mode="always"へ昇格）
│   │   │   └─ 収まらない → ~/.claude/rules/（横断ルール）
│   │   └─ 弱い行儀・好み → habits（intelligently層のまま）
│   │       （intelligently層は実際には引かれにくい実測がある。
│   │         本当に守らせたいものをここに置かない）
│   └─ 「知っとけ」系の文脈・tips？
│       ├─ 重要度高い → ~/.claude/CLAUDE.md
│       └─ 重要度低い → auto-memory
│           （同じ趣旨のfeedbackを繰り返し書いている場合は、横断ルールとして
│            ~/.claude/rules/への格上げをユーザーに提案する）
│
└─ どれにも当てはまらない / 複数に該当して迷う / 複数ルールが混ざっている
    └─ [rule-placement](../rule-placement/SKILL.md) のfull評価へ
```

## 格上げ則（判定フローに優先する例外）

「見逃すと不可逆な事故になる（外部へのpush・データ消失・他セッション巻き込み）」かつ「踏む直前に迷いが発生しない（良かれと思って・ついでに踏む）」ルールは、pull系（判例decision・habits intelligently層・auto-memory）に置いてはいけない。契機を名指しできるならその契機のpush経路（tag-notes/pin）、できないなら常時push（habits always昇格 or ~/.claude/rules/）へ格上げする。pull経路は実際には引かれないことがある。

## 判定の3軸（fast path版）

- **契機**: 何が起きたらこのルールが要るのか。ファイル → タグ → 作業単位の順に名指しを試し、名指しできないものだけを「常時」に落とす。「重要だから常時」は契機の名指しの失敗（重要度はリスク軸で扱う）
- **型**: 無条件規範（従え系）/ 条件付き判断基準（〜ならOK系）/ 事実・tips（知っとけ系）
- **リスク**: 見逃しコスト × 無自覚性。上記の格上げ則を参照

## habits投影の仕組み

`add_habit`で記録したhabitは常にtrigger_mode='intelligently'（マニフェスト表示）で作成される。SessionStart時に~/.claude/rules/へ自動投影されるが、intelligently層はタイトルのみで、全文は`get_habits`でのon-demand取得が必要になる。

「毎回必ず全文で見てほしい」レベルの制約は、記録後に`update_habit`で`trigger_mode="always"`へ昇格する追加ステップが要る（新規作成と同時にalways指定はできない）。判定に迷う場合はユーザーに確認する。

## 注入の強度

```
強 ┃ habits（always層）        — 全文をSessionStartで常時投影。絶対守れ
   ┃ rules/                    — セッション起動時に全文注入。従え
   ┃ pin                       — pin元へのcheck_inのたび本文ごと再配達
   ┃ tag-notes                 — タグ文脈に触れた初回に全文注入
   ┃ CLAUDE.md                 — プロジェクト文脈として参照
   ┃ 判例decision              — search/pull_precedents/add_ask時にpullで届く
   ┃ auto-memory               — 見えてはいるけど、気づけば使え程度
   ┃ habits（intelligently層） — タイトルのみ常時投影、全文はget_habitsで取得
弱 ┃
```

## 注意

- 判定に迷ったらrule-placementのfull評価へ。それでも決めきれない場合はユーザーに確認する
- 「決めたことの事実記録」としてのdecision（decision-record skillの担当）は「覚えて」の対象外。ただし条件付き判断基準の配置先としての判例decisionは本フローの正規の出口であり、対象外ではない。記録時はdecision-recordの書式作法（定型節）に従う
- auto-memoryとCALMの二重記録はOK（異なる注入経路として機能する）
- 過去の記録の訂正・撤回は対象外。矛盾・陳腐化に気づいたら[forget](../forget/SKILL.md) skillへ
