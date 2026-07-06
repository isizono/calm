---
name: audit
description: 【必須】過去 decision の正当性を疑った状況、同一 tag 内で 3 件目の方針変更 (supersedes 連鎖 / [議論中] 含む) を検知した状況、既決を見落として議論を始めかけた状況、同じバグを 2 回以上観察した状況、設計レビューで「この案前回却下した気がする」と感じた状況、ユーザーから「これ前に決めなかったっけ?」「また同じ話してる」「過去の情報と矛盾してない?」「グルグル回ってない?」「ちゃんと過去の議論を踏まえてる?」など過去判断への疑問・矛盾を表明された状況などで発動。一次リソース + 経緯 log を読み、全体像と文脈不足を分析し、知識を正しい場所 (tag note / habit / anchor / material / decision 改訂提案) に pin する長プロセス skill。このスキルを経由せずに decision の retract / supersede を直接提案してはいけない。TRIGGER: 上記の自発トリガー (T-A1〜T-A5) または ユーザー起点トリガー (T-B1〜T-B3) のいずれか。DO NOT TRIGGER: 設計議論中で挙動が未確定の論点 / 同主題 24h 内 audit 済 / ユーザーが「audit はいい」と明示拒否した直後 / 他 topic 管轄の仕組みのバグ観察 (それは cross-topic-bug-report)。
---

# audit

過去判断の正当性を疑い、一次資料 + 経緯 log で全体像を再構成し、文脈不足を分析した上で、再発防止の知識を正しい場所に pin する 1 サイクル。retract / supersede の直接実行は audit 範囲外で、decision 改訂提案として material に書き、ユーザー裁定経路へ降ろす。

「長いけどここまでしないと一生同じところでグルグルする」が本 skill の存在理由。各ステップは「飛ばさない」ことを優先する。

## 役割範囲

| 項目 | 内容 |
|---|---|
| 配置 | `skills/audit/SKILL.md` |
| 発動セッション | main / orch (思考の発端側) |
| 対象 | 過去 decision / 設計判断 / 同 tag の方針推移 (1 audit = 1 主題) |
| 種別 | 自律発動スキル |
| 重さ | 長プロセス (1 audit ≈ 1 セッション内、複数ターン消費) |

## 適用範囲

このスキルは初回 load 時に発動するが、トリガー判定は本セッション内のすべての後続ターンに適用される。以降のターンで T-A1〜T-A5 / T-B1〜T-B3 に該当する出来事が起きたら、明示的に skill を再発動せずとも本書の起動時確認フロー (§ 起動時確認フロー) に自動で入る。

ただし「同主題 24h 以内 audit 済」(T-C2) を確認したうえで、重複 audit は走らせず前回 material を参照する。判定方法は § 重複防止 を参照。

## トリガー

### T-A: 自発トリガー (claude 側状況認識)

| # | トリガー | 量の目安 | 判定根拠 |
|---|---|---|---|
| T-A1 | 過去 decision を引用して新規 decision 出した**直後**、引用元と整合か怪しいと感じた | 1 回でも | 引用整合性 |
| T-A2 | 同一 tag で **3 件目の方針変更**が検知された (supersedes / [議論中] 含む) | 3 件以上 | グルグル防止 |
| T-A3 | 議論で「あれ、これ前にも議論したような…」状態 | 1 回でも | 既決見落とし救済 |
| T-A4 | 同じバグ・症状を **2 回以上**目にした (構造問題の疑い) | 2 件以上 | 構造問題検知 |
| T-A5 | 設計レビューで「この案前回却下した気がする」と感じた | 1 回でも | 既決見落とし救済 |

### T-B: ユーザー起点トリガー (発話ニュアンス検知)

| # | 発話例 |
|---|---|
| T-B1 | 「これ前に決めなかったっけ?」「また同じ話してる」「またこの話か」 |
| T-B2 | 「過去の情報・自分の知っている情報と矛盾してない?」 |
| T-B3 | 「グルグル回ってない?」「ちゃんと過去の議論を踏まえてる?」 |

### T-C: 発動しない条件 (誤検知抑制)

| # | 状況 | 理由 |
|---|---|---|
| T-C1 | 設計議論中で挙動が固まっていない論点に対する自発発動 | 「方針変更」ではなく「探索中」 |
| T-C2 | 同 audit を 24h 以内に同主題で実行済み | 重複 (前回 material を参照すべき) |
| T-C3 | ユーザーが「audit はいい、進めて」と明示拒否した直後 | 明示否認尊重 |
| T-C4 | スコープが他 topic 管轄の仕組みのバグ観察である | `cross-topic-bug-report` skill の責務 |

## 起動時確認フロー

### 自発発動時 (T-A 系)

冒頭で以下を必ず提示し、**ユーザー承認を取ってから本体プロセスへ入る**:

```
[audit] 過去判断の正当性を疑う状況を観察:
- 発端: (T-A1〜T-A5 のどれが立ったか + 観察内容を 1-2 行)
- 想定対象: (audit 対象の decision / 設計テーマ / 同一 tag の方針推移の要約)
- 想定所要: 本セッション内で約 N ターン

audit を始めていい?
- A: 始める
- B: 後で / 今は不要
- C: スコープ調整して (対象を絞る・広げる)
```

A/B/C 形式で明示。B 選択時は何も記録せず終了 (空 audit 抑制)。C 選択時はスコープ調整→再確認→A。

### ユーザー起点 (T-B 系)

確認スキップで Step 1 (発端の明文化) から入る。発端の明文化時にユーザー発話を原文引用する。

### 重複防止

Step 2 (スコープ確定) の前に必ず以下を実行:

```python
search(keyword=主題キーワード, tags=["audit"], entity_type="material", limit=5)
```

- 24h 以内に同主題の audit material が hit → T-C2 重複と判定し、ユーザーに前回 material を提示して新規 audit は走らせない
- 同 tag の note 末尾に `#audited-YYYY-MM-DD` ハッシュタグマーカーがあれば、その日付が 24h 以内かもチェック
- 該当なし → Step 2 へ進行

## 長プロセス手順 (7 ステップ)

### Step 1: 発端の明文化

何が audit のきっかけか、claude が認識した状況・ユーザーが表明した疑問を**原文で記録**する。後のステップで「想定 vs 実態のズレ」を測る基準線になる。

- 自発発動 → T-A 番号 + claude 観察内容
- ユーザー起点 → T-B 番号 + ユーザー発話の原文引用

### Step 2: 対象スコープの確定

audit する**主題** (decision 1 件 / 設計テーマ / 同 tag の方針推移) を 1 つに絞る。複数主題は別 audit に分割。

- スコープ単位の例: `D#XXXX 単体`, `domain:cc-memory × tag:HintService` の方針推移, `topic#464 内の P2 関連 decision 群`
- スコープ広すぎ防止: 「1 audit = 1 主題」原則。複数論点が絡む場合は親 audit + 子 audit の分割を提案

### Step 3: 一次リソース取得

対象主題に関連する**一次資料**を集める。順序は固定:

1. **対象 decision の本文**: `get_by_ids(items=[{"type":"decision","id":...}])`
2. **対象 decision の supersedes チェーン**: `get_map(entity_type="decision", entity_id=..., max_depth=3)` で前後関係 (relation_type=supersedes フィルタが効くなら活用)
3. **anchor 参照先**: 該当 decision を pin している material (`search(tags=["anchor", domain_tag])`) があれば取得し anchor 対応表の検証先を読む
4. **コード anchor**: 「実装済」anchor のコードパスを `Read` で確認 (variable / function 名で `grep` 補完)

「最新 decision を静的参照」は不可 (setup-anchor の「anchor の型」と整合)。

### Step 4: 関連 log の一通り読み (経緯把握)

`get_logs(topic_id=...)` + `search(keyword=主題キーワード, entity_type="log", tags=[domain_tag])` で経緯 log を取得し**時系列で読む**。N 件・time-window 上限を設けて爆発を防ぐ:

- 上限: 直近 **30 件 / 90 日** のいずれか狭い方
- 読む観点: 「誰が・いつ・どんな状況で・なぜ判断を変えたか」「却下案の理由」「再開ループの兆候」

### Step 5: 全体像の整理 + 文脈不足の分析

ここが audit の**コア成果**。Step 3-4 を踏まえて以下を組み立てる:

#### 5-a. 経緯マップ (時系列)

```
- YYYY-MM-DD: 初出 (D#A) — 動機: ... / 制約: ...
- YYYY-MM-DD: 方針変更 (D#B が D#A を supersede) — 動機: ...
- YYYY-MM-DD: 再変更 (D#C) — 動機: ...
- 現在: D#C が活きている / D#A・D#B は supersede 済 / 議論中: D#?
```

#### 5-b. 論点の進化

論点が「同じ問い」を繰り返しているか、「新しい問い」が積み上がっているかを判別する。同じ問いの繰り返しが見えたら「グルグル」のサイン。

#### 5-c. 文脈不足の分析 (audit の真の付加価値)

判断がブレた原因を**構造**で語る。例:

- 「制約 X (cwd / 環境 / 上位仕様) が議論時点で把握されていなかった」
- 「同 domain の既決 D#Y を見落とした」
- 「decision 文面が抽象的で再解釈の余地を残した」
- 「anchor が古く、検証ができない状態だった」
- 「議論時点の前提が事実上覆っているのに decision が retract されていない」

「ブレた」だけで終わらせず、**次回ブレないために何が必要か** (anchor 強化 / tag note 追加 / habit 化 / 単発 decision 追加) まで一段踏み込む。

### Step 6: 検証結果の material 化

ここまでの分析を `add_material` で保存する (フォーマットは § audit material のフォーマット で詳述)。タイトル + content + tags + related を整える。

### Step 7: 知識の pin 先選定 + 実行

Step 5-c で出た「次回ブレないための知識」を、§ pin 先判定マトリクス に従って配置する。

- 自律実行 OK: `add_pin` / tag note `update_tag` / material `add_material` / habit `add_habit` の追加系
- ユーザー裁定経由: 既存 tag note の **書き換え** (削除側) / habit の **書き換え** / decision の retract 提案

実行後、変更内容を audit material の `## 知識の pin 先選定` セクションに記録する (どこに何を pin したかの台帳)。

### Step 7 完了後: 完了マーカー

audit 主題に対応する代表 tag の tag note 末尾に `#audited-YYYY-MM-DD` ハッシュタグマーカーを追記する。24h 重複防止 (T-C2) の判定に使う。

```
update_tag(tag="<代表tag>", notes="<既存notes>\n\n#audited-2026-06-22")
```

複数 tag に跨る audit の場合は、主題を最もよく代表する 1 つの tag にのみマーカーを書く (全 tag に書くと汚染が広がる)。

## audit material のフォーマット

### タイトル規約

```
audit: {主題の要約} ({YYYY-MM-DD})
```

主題が単一 decision なら `audit: D#XXXX 正当性検証 (...)`、設計テーマなら `audit: HintService × consistency_check 分離経緯整理 (...)` の形。

### tags

必須: `audit`, `reconsider`, `domain:<domain>`, `intent:audit`

`intent:audit` は専用 intent タグ。これに紐づく tag note が audit セッション中の振る舞いルール (decision 即時改訂禁止 / 中断時の下書き保存 / pin 先判定マトリクス遵守 / 完了マーカー) を session に注入する。

### source

```
audit skill 自発発動 (T-AX) / ユーザー起点 (T-BX) — 発端: ...
```

skill 経由で生成されたことを明示。後で audit material を集計するときに source キーで弾ける。

### content セクション構成

```markdown
## 発端
(T-A* / T-B* + 観察内容 or ユーザー原文引用)

## 対象スコープ
- 主題: ...
- 対象 entity: D#X, D#Y, A#Z, ...
- 関連 tag: ...

## 一次リソース
- decisions: D#X (本文要約), ...
- supersedes チェーン: D#X → D#Y → D#Z
- anchor 参照先: M#A (anchor 対応表 N 行目)
- コード: src/services/foo.py:bar

## 関連 log (経緯)
- L#A (YYYY-MM-DD): ...
- L#B (YYYY-MM-DD): ...
(時系列、上限 30 件 or 90 日)

## 全体像
### 経緯マップ
(Step 5-a 形式)

### 論点の進化
(同じ問い / 新しい問い の判別)

## 文脈不足の分析
(Step 5-c の構造分析)

## 検証結果
| 対象 | 判定 | 理由 | 改訂提案 |
|---|---|---|---|
| D#X | 維持 | ... | なし |
| D#Y | 文脈不足のため再表記推奨 | ... | retract→新規 / 本文加筆 / anchor 設定 |
| D#Z | 撤回候補 | ... | retract 提案 (ユーザー裁定) |

## 知識の pin 先選定
- tag note `domain:cc-memory` に追記: ...
- habit 提案: ...
- anchor 追加: M#A の anchor 対応表に N 行追加
- 新規 material: 該当なし (or M#B 作成)

## 完了マーカー
- 対象 tag: `<tag>` に `#audited-YYYY-MM-DD` 追記済

## 残課題
- (次の audit / 議論で扱うべき論点)
- (フォローアップ activity 候補)
```

### related (relations)

- 対象 activity (本 audit を呼んだ activity)
- 対象 decision / log
- 既存 audit material (再 audit の場合は前回 material と `related`)
- 対象 topic

### サイズ予算

上限なし。audit は長プロセスで詳細記録が価値。audit material は check-in 注入ではなく search hit 経由なので、recompose 統合 material と違ってサイズ予算の継続コストは低い。

### 中断・再開

1 セッション完走前提だが、中断時は audit material を「下書き」状態で保存し、再開時は前回 material を `update_material` で更新する。tag note `intent:audit` にこの方針を注入する。

content 冒頭に `**ステータス: 下書き (Step N で中断)**` を明記して中断点を残す。

## pin 先判定マトリクス

Step 7 の核となる判定。「正しい場所」とは、**知識を再利用するときに最も自然に出会える場所**。

### 4 候補の責務

| 永続化先 | 適用範囲 | 注入契機 | 更新コスト | 容量 |
|---|---|---|---|---|
| **tag note** | 特定 tag を持つ entity に遭遇したとき | tag が活きるセッションで自動注入 | `update_tag` で上書き | 中 |
| **habit** | 全セッション横断、tag 非依存 | SessionStart で全件注入 | `update_habit` で上書き、ユーザー承認必要 | 小 (件数制限) |
| **anchor (material 内)** | 特定合意の真偽判定先 | recompose-context 経由 or 個別 audit で参照 | recompose-context で再生成 / setup-anchor で更新 | 大 (material 単位) |
| **material (新規 audit 結果)** | 経緯・調査結果そのもの | search hit / 関連 entity 経由 | `update_material` で上書き | 大 |
| (参考) decision | 単発の合意事項 | get_decisions / search hit | 直接更新不可 (新規 + supersedes) | 小 |

### 判定フロー (claude 内部判定)

```
Q1: その知識は特定 tag に紐づくか?
  YES → Q2 へ
  NO  → Q3 へ
Q2: その tag の note は既に類似ルールを持つか?
  YES (類似ルール改訂) → tag note (update_tag)、ただし削除/書き換えはユーザー確認
  NO  (純粋追加)         → tag note (update_tag)、自律実行可
Q3: その知識はセッション横断で常時想起されるべきか?
  YES → habit、ただし新規 habit はユーザー承認必須
  NO  → Q4 へ
Q4: その知識は「合意の真偽判定先」(anchor) か?
  YES → anchor (recompose-context 連携 or setup-anchor 起動)
  NO  → Q5 へ
Q5: その知識は経緯・調査結果そのものか?
  YES → material (audit material 自体に収まる、別途追加 material 不要)
  NO  → 単発合意 → decision 改訂提案 (ユーザー裁定経由)
```

### 自律実行レーン vs ユーザー確認レーン

| アクション | レーン |
|---|---|
| tag note への純粋追記 (既存削除なし) | 自律 |
| tag note の書き換え / 削除 | ユーザー確認 |
| 新規 habit | ユーザー確認 |
| 既存 habit の更新 | ユーザー確認 |
| material 新規 (audit material 自体) | 自律 |
| material 更新 (anchor 対応表追加など) | 確証あれば自律 |
| anchor 新規/更新 | setup-anchor 起動経路 |
| decision retract / supersede 提案 | ユーザー裁定経由 (audit material に「提案」として記載のみ) |
| `add_pin` | 自律 (純粋追加のため) |
| `remove_pin` | ユーザー確認 |
| 完了マーカー (`#audited-YYYY-MM-DD` の追記) | 自律 (純粋追加のため) |

## 生成成果物

### 必須成果物

| 種別 | 件数 | 内容 |
|---|---|---|
| audit material | 1 件 | § audit material のフォーマット |
| log (audit セッションの経緯) | 1 件 | recording skill 経由 |
| 完了マーカー | 1 件 | 対象代表 tag note に `#audited-YYYY-MM-DD` 追記 |

### 条件付き成果物

| 種別 | 条件 | 内容 |
|---|---|---|
| pin (tag → material) | 主題の代表 tag が決まり、anchor 化が必要な場合 | `add_pin(source_type="tag", ...)` |
| tag note 更新 | tag note に書くべき横断ルールが新たに見えた場合 | `update_tag(notes=...)` |
| habit 追加 | tag 非依存の行動ルールが見えた場合 | `add_habit(...)` (ユーザー承認後) |
| decision 改訂提案 | 既存 decision を retract / supersede すべきと判断した場合 | audit material 内 `## 検証結果` テーブルに「改訂提案」として記載 (直接 retract はしない)。頻繁に参照されるのに定型節（`docs/precedent-format.md`）が無い decision に遭遇した場合は、supersede 再記録時に定型節（特に却下案・検証）付きの reason で書き直すことを提案に含める。単純な retract は却下理由・射程の情報を失うため、頻出参照の decision は supersede を優先する |
| 新規 activity 起票 | フォローアップ作業が必要な場合 | `add_activity(intent:implement / discuss)` |

## 関連 skill との境界

### 対比表

| 軸 | audit | recompose-context | setup-anchor | cross-topic-bug-report | postmortem |
|---|---|---|---|---|---|
| 動機 | 過去判断の**正当性疑い** | 累積情報の**整理** | anchor の**確定** | 他 topic への**バグ報告** | completed activity の**振り返り** |
| 対象スコープ | decision / 設計テーマ / 同一 tag 方針推移 | activity / topic / decision (関連グラフ全体) | 合意事項 1 件 | 1 観察事象 | 完了 activity 1 件 |
| 入口 | 自発トリガー T-A* / ユーザー T-B* | 手動 (「/recompose」「整理して」) or hint 誘導 | recompose 内部 or 単独 | 3 条件 AND 自発 | 手動 (「/postmortem」or activity 指定) |
| 出力 | audit material + pin 群 + 完了マーカー | 統合 material + anchor 対応表 + リコンサイル | anchor 対応表エントリ | log 1 件 | 反省ポイント material + 教訓永続化 |
| 重さ | 長 (1 セッション級) | 中 (整理単位次第) | 短 (合意 1 件) | 短 (log 1 件) | 中 (ステップ分解 + 対話) |

### 重なる動作の役割分担

| 動作 | 第一責務 | audit との関係 |
|---|---|---|
| 一次リソース取得 | recompose-context | audit も実施するが「過去判断の妥当性」の観点で読む (recompose は「最新統合」の観点) |
| anchor 設定 | setup-anchor | audit が anchor 不在を発見したら setup-anchor を呼ぶ (recompose 経由でも可) |
| 経緯 log 整理 | recompose-context / postmortem | audit は経緯を「ブレの原因分析」目的で読む (postmortem は「行動ステップごとの教訓抽出」) |
| 知識の pin (tag note / habit / anchor) | audit (固有) | recompose-context は anchor 対応表のみ更新、その他 pin 先選定は audit が担う |
| decision 改訂提案 | audit (固有) | retract/supersede は audit からの「提案」止まりで、決定はユーザー裁定 |

### 起動順序 (典型シナリオ)

- 「decision 引用時に怪しい」→ audit (自発 T-A1) → audit 中で anchor 不在判明 → setup-anchor 起動
- 「topic#X 全体を整理したい」→ recompose-context → 統合中にズレ発見 → audit を提案
- 「completed activity の振り返り」→ postmortem → 反省ポイントから知識永続化先迷う → audit の判定マトリクス参照 (任意)
- 「他 topic の仕組みバグ観察」→ cross-topic-bug-report (audit ではない)

## HintService との境界

audit skill は HintService (`src/services/hint_service.py`) とは**経路と重さが完全に分離**された設計。両者は同じ「過去判断との整合」関心領域だが、相互呼び出しはしない。

| 軸 | HintService | audit skill |
|---|---|---|
| 仕組み | 軽量 hint (info/warn) を hint type 値域から生成 | 長プロセス skill (人間対話 + 思考) |
| consistency_check | type 値域から削除済 | audit skill が代替担当 |
| 発火経路 | `check_in` 同期 / Stop hook 経由 additionalContext | description トリガー + ユーザー発話 |
| 永続化 | hint type 表現 + tag note 内ハッシュタグマーカー (`#audited-YYYY-MM-DD` 等の suppress 用途) | audit material + 各種 pin |
| severity | info/warn のみ (block 不採用) | severity 概念なし (skill は手続き) |
| orch_managed=True activity | 全 suppress | 発動可 |

完了マーカー `#audited-YYYY-MM-DD` は HintService 側 hint 重複抑制と共通の仕組みを意図しているが、audit skill 自身の 24h 重複防止 (T-C2) の判定にも使う。

## Edge Cases

| ケース | 期待挙動 |
|---|---|
| audit 対象 decision が retract 済 | audit material の `## 検証結果` に「対象は既 retract」と記録し、supersede 先の決定が今も妥当かを副次 audit |
| 対象 topic が他セッションで並行修正中 | 並行修正の log を Step 4 で拾い、現在 in-flight な議論を `## 残課題` に明示 |
| 一次リソース (コード) が存在しない (anchor リンク切れ) | `## 文脈不足の分析` に「anchor が剥がれている」と記録し、setup-anchor 起動候補としてマーク |
| 「過去 decision を引用」した直後で実際にはマッチしていた (T-A1 誤検知) | Step 1 の発端明文化時点で「マッチ確認済」と書き、Step 2 でスコープなしとして audit 中断 (空 audit) |
| ユーザーが Step 5 途中で「もういい、わかった」と中断 | 部分的な audit material を「下書き」として保存 (§ 中断・再開) |
| audit を recompose-context が呼んだ場合 | recompose 中の発見 (ズレ・矛盾) を発端として audit に降ろす経路は OK。audit 完了後に recompose に戻る |
| skill 実行中 (他 skill 実行中) に audit トリガー | 現在 skill 完了まで待ち、終了後に audit 起動。skill 入れ子は禁止 |
| 同 decision を 2 回 audit (T-C2 抜け) | T-C2 重複検知が漏れた場合は Step 2 (スコープ確定) で前回 material を発見し、重複と判定して `## 残課題` 引き継ぎのみで終了 |
| HintService 側で consistency_check 完全削除済なのに過去の hint 残骸が見える | 残骸を発見したら HintService 側のバグとして `cross-topic-bug-report` に降ろす (audit の範囲外) |
| 完了マーカー追記対象の tag が複数候補ある | 主題を最もよく代表する 1 つの tag にのみ追記 (全 tag 汚染回避) |
