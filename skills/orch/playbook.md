# orch 一般プレイブック

## 本書の位置づけ (4層構造)

本書は orch が参照する情報4層 (orch SKILL.md §1 参照) のうち **Layer 3 (一般 playbook)** である。

- Layer 1: §0 不変責務 (orch SKILL.md §0) — 状況非依存の orch identity
- Layer 2: §2+ プロトコル仕様 (orch SKILL.md §2以降) — envelope / state machine / heartbeat 等の機械契約
- **Layer 3: 一般 playbook (本書)** — 全プロジェクト共通の運用流儀
- Layer 4: 特化版 playbook (cc-memory material、タグ `playbook`+`domain:<>`) — リポ固有ハウスルール

特化版がある章は特化版で本書を上書きする (章名キー突合、同名章は特化版優先)。本書は特化版がカバーしていない項目に適用される。

---

## モデル選択

`command:assign` の `model` 必須。**claude-opus-4-7 のみ許可** (タスク性質による使い分けはしない)。

- 全タスク共通: `claude-opus-4-7`
- sonnet (`sonnet` / `claude-sonnet-4-6` / `[1m]` 付き等) は禁止 (credit消費が大きいため、ユーザー裁定)
- haiku も禁止 (本playbookで上書き)
- opus 4.8 (`opus-4-8` / `claude-opus-4-8`) は恒久禁止 (ユーザー裁定)
- `opus` `opus-4-7` 等のエイリアスは `claude-opus-4-7` に正規化される

worker の SA (サブエージェント) にも同ルールを適用する。1Mコンテキストモード等のリポ別調整は特化版で上書きする。

## 思考worker (effort指定) の使い分け

`ow_spawn_worker(effort=...)` を指定すると思考worker (深い議論・設計検討・調査向け) として起動する。値: `high` / `xhigh` / `max` / `ultratink` (sentinel; ow_service内で正規綴りに alias される)。

| effort | 用途 |
|---|---|
| `high` | 通常より深く考えてほしい議論・設計検討 |
| `xhigh` | 多角的な比較検討・トレードオフ整理 |
| `max` | 大規模な設計判断・複雑な仕様確定 |
| `ultratink` (sentinel) | 最深長考。仕様の根本検討・抜本的設計レビュー等 |

挙動 (effort 指定時):
- task_file 本文に思考トリガー語マーカーが正規綴りで埋め込まれ、worker セッション全体が長考モードで動作する
- frontmatter に `effort: <値>` が残る
- OW_TERMINAL=tmux のとき、通常worker (split-pane) ではなく `tmux new-window` で別タブに開かれる
- 対応 activity には `intent:thinking` タグも付与する

orch 側ドキュメント・チャット出力では sentinel `ultratink` (意図的タイポ) で参照する。orch セッション自身に正規綴りが入ると extended thinking が暴発するため。`ow_spawn_worker` の `effort` 引数にも sentinel をそのまま渡してよい (ow_service が正規化する)。

## orch 自律実行の範囲

orch は受動報告器ではない。以下を自走する:

- **blocker なしならそのまま着手**: task が available で blocker が無ければ「やる？」と聞かずそのまま着手・自走する。提案フェーズを挟まない
- **即アクション**: 単純依頼は確認せず即時実行 (例: 「紐づけだけ」「ステータス更新だけ」)
- **デフォルトは自走、権限境界のみ確認**: 特化版 playbook で定義された権限境界を超える操作のみユーザー確認する。権限境界未定義のリポでは、自走可能な範囲を広く取る
- **長時間自律ガード**: 自走指示中も判断停止しない。インフラ状態が変化したら (relay 落ち / worker crash / cache 不整合) 再 spawn を判断する
- **orch は手を動かさない**: 実装・コード変更・調査用の Bash・git 操作は worker に委譲する。直接実行可は ow_close_worker / ow_send / ow_spawn_worker / 状況報告 / worktree 作成と git 段取り のみ
- **外部投稿禁止**: PR コメント・GitHub への直接投稿はしない (worker / SA経由)
- **close 判断は orch 一存**: terminated 受領後の ow_close_worker 判断は orch の責務。人間に振らない

## worker spawn の段階原則

問題に対していきなり実装worker を spawn してはならない。**3段階に分けて spawn する**:

1. **問題箇所特定**: バグ・課題の原因箇所を特定する worker (調査・原因分析)
2. **対策設計**: 特定した箇所に対する修正方針を設計する worker (設計・トレードオフ整理)
3. **実装**: 確定した方針に基づく実装 worker (コード変更・テスト)

背景が掴めないまま実装PRを出すのは禁止。背景把握→対策設計→実装の順を守ることで、的外れな実装による手戻りを防ぐ。

## 1 worker = 1 activity 原則

各 worker は自分専属の activity を1つ持ち、その activity 以外への書き込みは禁止する。

- worker は自分の担当 activity に対してのみ log / material / check-in を行う
- 他 worker の activity / orch 管理 activity / 共通 activity への書き込みはしない
- 例外: エスカレーション時に人間がそのworkerセッションで合意した decision (worker SKILL.md §エスカレーション 参照)

## 並行設計workerの合流点同期パターン

複数の設計worker が並行で議論している場合の合流点 (中間合意・最終合意) での同期パターン:

- **batch 転送**: 各 worker の中間決定事項は、他 worker への転送をbatch化する。逐次転送するとmsg数が爆発する
- **転送前 ow_history 確認**: 転送前に各 worker の最新 ow_history を確認し、すでに伝わっている内容を二重転送しない
- **入れ違い即訂正**: 並行で送られた相反する提案を発見したら、orch が即訂正命令を batch 送信する

## Trouble-shooting (運用症状一般)

### heartbeat 30秒周期が tool busy 中に停止する

worker が長時間ツール実行中 (例: 大量ファイル grep / 長時間 SA spawn) は heartbeat が止まることがある。途絶検知時はまず `command:ping` 送信 → pong (state echo) で生存判定する。pingに応答があれば生きている。無応答かつ heartbeat 復活なし → crash 推論経路。

### worker完結 auto-assign が不発になる

ready 状態のworker に対する auto-assign が不発になることがある。`ready` 60秒経過しても assign が処理されない場合は、明示的に `command:assign` を再送する。

### 隣 orch 併走検知

`ow_status` の presence で同 channel に他 orch handle (orch / orch-*) が検出された場合は、即座に人間に通知する。1 channel = 1 topic = 1 orch 原則違反。channel 再作成 or 片方の退場をユーザー判断で決める。

### tmux workers 可視性

`OW_TERMINAL=tmux` 環境では、orch が自身の `os.environ['TMUX_PANE']` を `ow_spawn_worker(..., tmux_target_pane=<TMUX_PANE>)` に渡すことで、同 window 内に worker pane を分割表示できる。未指定時は別 session で起動するため、ユーザーが worker の画面を見られない。

## worker 生死管理

- **orphan は orch が自律判定**: identity に登録があるが cache.workers 外の handle、または cache.workers に active で残っているが identity が terminal な handle が見つかったら、orch が以下3ステップで判定する:
  1. heartbeat が古いか (途絶検知閾値超え) → crash推論
  2. 過渡状態か (loading / draining / state遷移直後) → ping で生存確認
  3. それ以外 → ping で素性照会、応答で再リンク or 退場処理
- **外部 worker は想定外**: orch が spawn していない worker (外部参加) は想定外。channel に出現したら即通知し、人間判断
- **channel = 1 orch + 1 topic**: 同 channel に複数 orch / 複数 topic の worker は混在させない

## spend limit hit 時のサルベージ手順

worker (または orch 自身) が spend limit (Claude Code の API quota) に達して停止した場合のサルベージ手順:

1. `git status` / `git diff` で未コミット変更を確認
2. WIP コミット & push (中途半端でも commit して push、ローカル消失防止)
3. サルベージ log を担当 activity に記録 (どこまで進んだか・残作業)
4. `ow_close_worker(term_ref)` で worker セッションをクローズ
5. activity description に「spend-limit中断、次回再開時の手がかり」を追記
6. 次セッションで再 spawn (orch が新 worker を立てて続きを assign する)

## SA / worker 分担基準

- **複数ファイル横断はSA委譲**: 1ファイル内の単純編集はメインでもよいが、複数ファイル横断の編集は SA に委譲
- **cwd 絶対パス必須**: SA spawn 時の cwd は絶対パスを渡す。`~` 展開はSA環境では効かない
- **SA モデル選択**: 機械的タスクでも実装でも設計でも、本playbook方針 (sonnet/haiku 禁止) により **claude-opus-4-7 一択**。一般的なSAスキル定義での「機械的→haiku/sonnet」「設計→opus」分類は本playbookで上書きされる

## シェル経由 CLI起動のデバッグ手法

CLI コマンドを bash 経由で叩いた際、UI 上に何も出ない時は「コマンドが届いていない」と即断しない。

- **argv が一次情報**: bash 側で実際に渡された argv (echo / ps / strace 等) を確認する
- UI表示の有無では「コマンド届いていない」と判定しない
- shell escape / quoting で argv が崩れているケースが多い

## 議論 / 設計タスク要否判定 (What / Why / How)

新規タスクが議論型 (discuss) / 設計型 (design) / 実装型 (implement) のどれかを判定する基準:

- **What / Why / How が揃っているか確認**: ユーザーが提示した「やりたいこと (What)」「なぜそうするか (Why)」「どう実現するか (How)」が揃っていれば実装フェーズへ
- Why が曖昧なら議論フェーズ (intent:discuss タグ付与)
- What / Why は確定しているが How が未確定なら設計フェーズ (intent:design タグ付与)
- What だけ確定で Why が曖昧な場合は危険。先に Why をユーザーに確認

## cwd 絶対パス必須 / ~ 非展開

- `ow_spawn_worker` の `cwd` は絶対パス必須 (`~` 展開はサーバープロセスで効かないことが多い)
- SA spawn 時の cwd も同様に絶対パス
- worktree 内で worker を立てる場合は `/Users/<user>/workspace/<repo>/.trees/<branch>/` のような絶対パスで指定

## silent failure 防止

- アダプタ (tmux / iterm2 / 外部 CLI 呼び出し) は **実機検証必須**。docs だけで通さない
- エラーをサイレントに握り潰さない。失敗は warnings / error として返す

## Co-Authored-By: Claude を worker commit に必ず付ける

worker が作成する commit には以下の trailer を必ず付与する:

```
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

orch から worker に渡す context / playbook 抜粋にも明記し、worker が忘れないようにする。

## acceptance 起草前の既存 decision 整合確認

新規 acceptance を worker に渡す前に、既存 decision との整合を確認する:

- 関連トピックの `get_decisions` で過去合意を取得
- acceptance 文言が既存 decision と食い違っていないか確認
- 食い違いがあれば acceptance を修正 or 既存 decision を supersedes してから assign

## タイムアウト既定値

### worker assign の timeout_min

- デフォルト値: **10分**
- assign に明示指定しない場合は10分を適用する
- タスクの性質 (大規模実装・長時間調査等) に応じて orch が上書き可能

heartbeat 途絶 watchdog の閾値表は orch SKILL.md §watchdog で定義されており、本playbookでは重複させない。

## worker同時稼働数上限

- **最大5インスタンス** (暴走防止ハードリミット兼用)
- 実行中 (in_progress/assigned/spawning/awaiting_verify) のworker数が5に達している場合、新規 spawn は待機キューに留める
- escalated はカウントに含める (セッションが存続しているため)
- stalled はカウントに含める (閉じていないため)

## エスカレーション基準

worker がエスカレーション (`event:state(blocked)` → `command:answer {escalate: true}`) を要請する典型的な場面:

| 類型 | 例 |
|---|---|
| 要件の曖昧さ | acceptanceの解釈が複数あり、どれが正しいか判断できない |
| 破壊的操作の確認 | 本番データの変更、リポジトリへのforce push等 |
| 予算・権限外の操作 | acceptance範囲を大幅に超える実装が必要になった |
| 外部サービス・API変更 | 依存ライブラリの破壊的変更や外部APIの仕様変更を発見した |
| 設計上の矛盾 | 実装中に設計書の矛盾・誤りを発見し、一人では判断できない |
| セキュリティ懸念 | 脆弱性の可能性があり、修正方針に人間の判断が必要 |

エスカレーションフォーマット (質問/推奨と理由/選択肢/文脈要約/関連ID) で出力する。

orch が自力で判断できる場合は `command:answer {answer}` で直接回答し、エスカレーションに進めない。escalate 指示は慎重に使う。

## 報告頻度

| タイミング | 内容 |
|---|---|
| 状態変化時（即時） | タスクボード短報（status変化、blocked/escalated発生、done検証結果等） |
| worker spawn時 | worker起動確認（alias、task名、model、cwd） |
| done検証完了時 | acceptance照合結果 + decision_proposalsの採否 |
| crash復旧完了時 | 整合チェック結果と復帰状況のサマリー |

定期的なポーリング報告は不要。Monitor 発火ベースのイベントドリブン報告を原則とする。

タスクボード短報・状況報告・AskUserQuestion でタスクを言及する際は、内部タスク識別子 (`T<n>`) 単独使用は禁止。ユーザーが直接判断できる名前 (機能名 / activity_id / PR番号 等) を必ず併記する。
