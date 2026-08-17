# CALM 抽象レイヤー仕様書 v0 ドラフト

CALMを「コードを開かずに議論できるレイヤー」で写し取った作業用ドキュメント v0。各章はspec（現状の写し取り）とplaybook（こう使われるべき行動規範）の2部構成。凍結を目的としない議論ベース。

---

## 0. 読み方

この文書はCALMをコードファイル名や関数名を出さずに、4層の抽象モデルに沿って写し取った作業用ドキュメントである。仕様凍結を目的としない。前提として「仕様凍結はデータセマンティクスまで」という既存方針があり、MCPツールシグネチャ等は凍結対象から外している。

**各章の構造:**

- spec: いま実装としてどう動いているか、何が概念として存在するかの写し取り
- playbook: そのspecに対して「こう使われるべき」「こう使わないとこうなる」の行動規範
- 各specの末尾に **アンカー** を付ける。アンカーは「今も正しいかどこを見れば検証できるか」の生きた参照先で、コードベース / 既存決定 / 既存資料の3種類で論理名で書く（CALM内部IDは本文に出さない方針）

**この文書のリビジョン:**

CALM本体のmaterialとして保存され、supersedesリレーションで v1, v2 と版交代する。docs/配下のmdファイル化はユーザー要望により並行運用する。

---

## 1. 全体像

### 1.1 spec: 4層スタック

CALM（広義）は4層のレイヤースタックで整理される。

| 層 | 比喩 | 中身 | 「仕事」 |
|---|---|---|---|
| プロトコル | 紙の上の約束 | エンティティ型 + 関係 + supersede/retract | 共通の意味論 |
| ストア | 書庫 | データの保持と読み出し | 読まれること |
| フロー | 働き方 | check-in/scoring/nudge/habits/tag-notes | 動くこと |
| 協調 | 指揮系統 | orch/worker（+powwow） | セッション間調停 |

CALMには「記憶の保持（半年後も価値が変わらない）」と「タスク管理（今日のユーザーを動かす）」という2役問題がある。これを概念上「ストア層 vs フロー層」で区別する。実装（プロセス・DB）は分割しない。分けるのは概念とインターフェースの帰属のみ。

層の独立性: 各層は独立に差し替え可能。例: フロー層を全部入れ替えてもプロトコル層は生きる。協調層を廃止してもストア・プロトコルは生きる。

**アンカー:**

- 既存資料: プロダクト群マップ v1（CALM資料）、4層スタック決定（topic「プロダクト群マップ」配下）
- コードベース: cc-memoryリポ直下のディレクトリ構造（services/ hooks/ skills/ migrations/）が大まかな層対応

### 1.2 playbook: 議論時に「どの層の話か」を最初に宣言する

CALMに関する議論を始めるとき、「これはどの層の話か」を最初に明示する。明示しないと処方箋の所在を誤る。

**典型例:**

- 「hint発火条件をこう変えたい」 → フロー層の話
- 「retract後にKNN検索が劣化する」 → ストア層の話
- 「supersedesのセマンティクスを変えたい」 → プロトコル層の話
- 「workerがdecisionを直接書いてしまう」 → 協調層 × プロトコル層の接点の話

**やらないとどうなる:** 層をまたいだ症状（例: 「retract仕様を変えればKNN検索も自動で正しくなる」と思い込む）に対し、処方箋が片方の層に偏る。プロトコル層を直してもストア層の物理削除が必要、というケースを見落とす。

---

## 2. プロトコル層（記憶のセマンティクス）

### 2.1 spec: エンティティ型 / 操作 / 関係 / 凍結対象

**エンティティ型:**

- **topic**: 1つの関心事・問題・機能。タグで整理される議論の容れ物
- **decision**: 結論。reasonとセットで記録される
- **discussion_log（log）**: 議論の経緯・議事録。結論に至る道筋
- **activity**: 作業の単位。「SV（主語+動詞）で何をするか表せるもの」が判断基準
- **material**: 成果物・ドラフト・調査結果。要約せず生データを保存する

**関係:**

- **relation**: 双方向の関連（topic↔activity、material↔activity 等）
- **supersede**: decision間の上書き関係。旧decisionを差し替える
- **depends_on**: activity間のブロッカー関係
- **pin**: 任意エンティティの強調。重要なものを引っ張りやすくする

**ライフサイクル操作:**

- 作成（add_*）→ 修正（update_*）→ retract（論理削除）/ supersede（後継で置換）
- retracted_at列を持つのはdecisionとlogのみ。material/topic/activityには現状ない

**タグ:**

- 名前空間付き: `domain:`（プロジェクト）/ `intent:`（作業意図）/ 素タグ（キーワード）
- tag-notes: タグに紐づく教訓・運用ルール。AIに自動注入される

**凍結対象:**

- データセマンティクスまで（型・関係・操作の意味）
- 凍結しない: MCPツールシグネチャ、引数仕様、出力フォーマット

**アンカー:**

- コードベース: cc-memoryリポのmodels/、migrations/
- 既存決定: 3プロトコル決定、データセマンティクス凍結方針決定
- 既存資料: プロダクト群マップ v1 §3, §5

### 2.2 playbook

#### decision vs log の使い分け

- **decision = 結論**: 「何にどう決めたか」+ 「なぜそうしたか」のreason。半年後の自分が読んでも判定理由が辿れる粒度で書く
- **log = 経緯**: 結論に至る議論の流れ、ユーザー発話、採用しなかった選択肢
- 結論だけ書いてreasonを薄くしない。「適宜」「必要に応じて」のような曖昧表現を避ける

**やらないとどうなる:** 結論だけ残るとなぜそう決めたかが消える。次のセッションが同じ議論を繰り返す。

#### activityの2つの顔

- **記録の顔（過去形）**: 事実・文脈ハブ・check-inの読み出し単位。複数フローで意味が同じ → プロトコル帰属
- **タスクの顔（未来形）**: status遷移・scoring・nudge。フローごとに違ってよい → フロー層ローカル

**判定基準:** 「複数フロー（個人運用 / orch運用 / 将来の他者運用）で意味が同じならプロトコル、フローごとに違ってよいならフロー層」

**やらないとどうなる:** orchフローのqueueとactivity.statusが二重管理になり、どちらが真かが不明瞭になる。

#### retract / supersedes ライフサイクル

- retractは論理削除（retracted_at立てる）。supersedesは後継decisionで置換
- 既知の問題: 論理削除の連鎖が閉じていない（search_index物理クリーンアップなし、material/topic/activityにretracted_at列がない、関連pin/relationの扱いが不明瞭）
- プロトコル層では「論理的に消えた」と宣言するだけ。Read経路への伝播はストア層責務

**やらないとどうなる:** retract後も検索結果に出続ける、KNN検索のrecallが劣化する。

#### pinの判断基準

- 長期にわたって参照され続けるエンティティをpinする
- 例: ユビキタス言語を定義したmaterial、方針を決定づけるdecision、各セッションが共通参照する設計書
- タスク単位の一時的注目はpinしない（pin膨張で注入予算を食う）

**やらないとどうなる:** pinが本当に重要なものでなくなり、check-in時の優先度シグナルとして機能しなくなる。

---

## 3. ストア層（CALM狭義 = 記憶の保持）

### 3.1 spec: データの保持と読み出し

**保持:**

- SQLite + FTS5（全文検索） + embedding（vec_index、意味検索） + relation（グラフ）
- タグ二段防御: tag_canonicalsで正規化、エンティティ-タグの結合テーブル
- retract遅延除外: search経路で `NOT EXISTS` 句で論理削除を弾く（物理削除は別問題）

**読み出しツール:**

- search: FTS + vec のRRF（Reciprocal Rank Fusion）+ recency乗算。タグフィルタAND/OR、ドメイン絞り
- get_by_ids: 詳細チェリーピック（最大20件）
- get_material / get_logs / get_decisions / get_timeline / get_map: エンティティ別の専用取得
- check_in: アクティビティに紐づく文脈を一括取得（tag-notes / 資材カタログ / pinned / 関連decisions / recent logs）

**coverageの概念:**

- check_inが返す「どれだけ情報を引けたか」のメトリクス
- 低カバレッジ項目（特にlogs）は議論の経緯を含むため追加取得を推奨する設計

**スコア:**

- 0-1正規化。1.0 = 全検索ソースで1位。0.4以上=高、0.15-0.4=中、0.15未満=低
- 既知の問題: 「正規化→recency乗算」で解釈が崩れる（5次元統合レポートで指摘）

**アンカー:**

- コードベース: cc-memoryリポのservices/search_service、services/material_service他
- 既存資料: 5次元統合レポート Read Path 章（横断課題）

### 3.2 playbook

#### 何を保存し、何を保存しないか

- 保存する: 半年後に「どうしてその結論になったか」を辿る必要があるもの → decision + log
- 保存する成果物: SAの出力・整理結果・長文ドラフト → material（要約せず生データ）
- 保存しない: 一過性のスクラッチ、揮発OKの中間状態
- **迷ったら保存**。保存しすぎて困ることはない。保存し忘れた情報は二度と戻らない

**やらないとどうなる:** materialとして残らなかった成果物がセッション終了で揮発する。後から「あの調査どこ行った」を再現できない。

#### tag namespaceの使い分け

- `domain:` プロジェクトスコープ（domain:calm）。必須
- `intent:` 作業意図（intent:discuss/design/implement）。activityで必須
- 素タグ: 内容キーワード（pin / recompose-context / sync-memory 等）。積極的に付ける
- tag-notesは素タグや `domain:` に紐づける。AIに自動注入される

**やらないとどうなる:** タグがバラつくと検索ヒット率が下がる。tag膨張で注入予算が枯渇する（横断テーマT-C補完チャネル重複と接続）。

#### 検索とpullの規律

- 最初は searchでキーワード探索 → get_by_idsで詳細チェリーピック（2段階リード設計）
- searchはlimit指定と score閾値で判断（0.4以上を信頼基準）
- 同じ検索を繰り返す自分に気付いたら → pin候補のサイン
- search_tagsで関連タグを先に確認してから tagsフィルタを絞ると精度が上がる

**やらないとどうなる:** 1段階目で全文取得して context を食う。検索が当たらず2回3回と空振りを繰り返す。

#### Read系ツールの選び方

- get_by_ids: search結果のチェリーピック / ID指定の確認（materialの場合はcontent/sourceも含まれる）
- get_material: material_idだけ手元にあり概要も含めて取りたい単発ケース
- get_logs / get_decisions: エンティティ深掘り
- get_timeline: 時系列変遷
- get_map: 関連構造の俯瞰
- check_in: 作業開始時の文脈ロード

**やらないとどうなる:** 同じ情報を別ツールで何度も取って context を圧迫する。例えばmaterialならsearch→get_by_idsで完結するところを、不要な get_material を続けて呼んでラウンドトリップが増える。

---

## 4. フロー層（働き方）

### 4.1 spec: check-in / status / scoring / nudge / habits / tag-notes の論理像

**check-in:** アクティビティに紐づく文脈を一括取得する入り口。返却内容は tag-notes、資材カタログ、pinned、関連decisions、recent logs、coverage。セッション内で初めて呼ばれたときのみコンテキスト取得フローガイド（flow_guide）も返す。statusはin_progressへ自動更新される。

**status:** pending / in_progress / completed / snoozed / shelved の5値。「active」はpending+in_progressのエイリアス。

**scoring:** SessionStart時に未完アクティビティを優先度付きで提示する。判断基準: depends_on未完=減点、締切近=加点、自分がブロッカー=加点、鮮度=やや加点。

**nudge:** 記録忘れ防止のシステムリマインダー。

- record nudge: 2ターンごとに発火、記録ツール（add_decisions/add_logs/add_topic）の呼び出しを促す
- follow_up nudge: add_decisionsを呼んだが補完エンティティ（topic/logs/activity/material/tag_notes）が更新されてない時に発火
- recompose hint: 素タグに対するrecompose-context実行のメンテナンスナッジ

**habits:** 全セッション共通の行動ルール。タグやファイルに依存しない横断ルール。正はDBで、`trigger_mode='always'`は`~/.claude/rules`配下の自動生成ファイル経由で全文配信、`'intelligently'`はタイトルのみのマニフェスト表示（詳細は`get_habits(habit_id=...)`でon-demand取得）。SessionStart hookは投影ファイルの鮮度検証と縮退フォールバックのみを担う。

**tag-notes:** タグに紐づく教訓・運用ルール。そのタグに遭遇したとき（セッション内初回）にAIへ自動注入される。CLAUDE.mdのタグ版。

**sync-memory:** セッション終了前に transcriptを解析し、トピック・決定事項・ログ・アクティビティを一括記録・更新するスキル。

**アンカー:**

- コードベース: cc-memoryリポのhooks/、skills/
- 既存資料: 5次元統合レポート フック章・スキルIF章
- 既存決定: nudgeエスカレーション仕様、SessionStart構造方針

### 4.2 playbook

#### check-in入り口の使い方

- 作業開始時は必ずcheck-in。引数なしなら未完アクティビティ提示、引数ありで直行
- **pinned情報は最初に確認**。注入チャネルとして優先度最高（任意ピン留め＝長期重要のシグナル）
- hintsフィールド（recompose-context実行を促すナッジ）はユーザーへの提案として末尾に提示
- coverage低項目は追加取得を検討（特にlogsは経緯を含むため）

**やらないとどうなる:** tag-notesや過去のdecisionを見落とし、解決済み議論を再演する。「手のひら返し」（前回検証済みの結論をSA再調査で覆す）が起きる。

#### skillsとの三層責務分担

- **man = pull型説明**: ユーザーが「使い方教えて」とpullしたとき発動
- **SessionStart = 静的認知**: 毎セッション同じものを注入（habits投影ファイルの鮮度検証 + アクティビティ一覧 + 鮮度警告）
- **nudge = 文脈依存の機会提示**: シグナル検出 → 提案（pin提案、recompose提案、記録忘れ警告）

**やらないとどうなる:** manに動的情報を入れすぎる、SessionStartにcontext依存情報を入れて毎回スキャンする、nudgeで静的情報を再注入する、といった責務混線で注入予算が浪費される。

#### hint/nudge発火原則とノイズ制御

- 1ターン1件上限
- セッション内で同種を再発火させない（_shown_consistency_hints等の状態管理）
- nudge増殖（同じ文言を5回繰り返す等）は最終手段。常用すると無視耐性が育つ
- 観測知見: 46ターン中16回発火→全て無視→記録ゼロの事例がある（増殖だけでは規律が降りない）

**やらないとどうなる:** 注入チャネル膨張（横断テーマT-C）、tag-notesとhabitsの増殖でコンテキスト予算枯渇。

#### sync-memoryの位置

- セッション終了前に経緯log・生データmaterialを記録する自動化
- workerは退場時にworker-syncで簡易sync（decisionはorchへ提案、自分で書かない）
- decisionは「双方の合意」が要るが、materialは「成果物が出た時点で確認なし保存」が原則

**やらないとどうなる:** transcript内にしか存在しない文脈が揮発する。次のセッションが原文脈をpullできない。

#### intentの完了条件と境界

- `intent:discuss` = What/Why/Scope/Acceptanceを明確化する。実装に入らない。代替案を出し矛盾を指摘する
- `intent:design` = How/Interface/Edge casesを確定する。コードを書かない
- `intent:implement` = コード + テスト + PR + 検証

**境界を越えるならactivityを切り直す**。intent タグだけ書き換えない（discuss→designで既存activityをcompleted→新規activityでdesign開始）。

**やらないとどうなる:** discussで設計判断を内包したまま実装に入って後戻りする。「何を合意したか」と「何を実装するか」が混ざって履歴が辿れなくなる。

---

## 5. 協調層（orch/worker × CALMの接点）

### 5.1 spec: CALMに関わる接点のみ

orch/worker フレームワーク全体ではなく、CALMと接する3点に絞る。

**接点1: 記録ガード（worker → orch エスカレーション）**

workerはdecisionを直接 add_decisions しない。仮合意した内容を relayメッセージとして orchへ提案する。orchが受領 → 検証 → CALMへ記録する。

**接点2: orch-managed の扱い**

orch運用下で生成される activity/decision/material 等には `orch-managed` タグを付与する。個人セッションの SessionStart 一覧、scoring、nudge から除外する。タグ運用ベース（コード強制はまだ）。

**接点3: サブセッション間の文脈分断**

複数Claude Codeセッションが共有する基盤としての側面。各セッションが一部の文脈しか見えない・ユーザーしか知らないコンテキスト・別セッションが先に進めた決定の見落とし、などが起こる。

**アンカー:**

- コードベース: cc-memoryリポのhooks/session_*（v1通信系の`services/ow_service`は撤去済み。後継はrelay v2 4動詞tool、`src/services/relay/` + 依存パッケージ`relay_sdk`）
- 既存資料: ow統合設計書 v3、orch役割境界 設計書 v2、5次元統合レポート マルチセッション章
- 既存決定: 「orchは原則手を動かさない、workerに任せる」習慣、「workerはdecisionを直接書かない」習慣

### 5.2 playbook

#### decision記録ガード

- workerが議論ターン中に「これ合意した」と思ったら、relayの `decision_proposal` 等の宛て先でorchへ送る
- workerが直接 add_decisions すると、orch視点で「いつ、何を、どのworkerが書いたか」が追えなくなる
- 例外: worker自身の作業logは workerが直接 add_logs してよい（経緯記録）

**やらないとどうなる:** workerがコンテキストを勝手に汚染する。orchが知らないdecisionが既成事実化する。

#### orch-managedの扱い

- orch生成 activity は `orch-managed` タグ付与必須
- 個人セッションのSessionStartは `orch-managed` を含まないクエリで一覧を返す
- 既知の問題: タグ運用依存で機械的整合が取れていない（横断テーマT-A「文面の規律」の典型）

**やらないとどうなる:** orchが量産したactivityが個人セッションのscoring/nudgeに混入し、ユーザーが「自分のタスク」を見失う。

#### 文脈分断の抑制

- 設計合意は必ずdecision化（口頭合意は揮発する）
- 大きい議題は topic 化、中間整理は material 化
- セッション終了前に sync-memory を実行（経緯log + 生データmaterial）
- 別セッションが進めた決定は check-in時の delta通知（実装中）で気付ける設計

**やらないとどうなる:** 別セッションが先に進めた決定を見落とし、SAに再調査させて検証済みmaterialを引かず、「手のひら返し」を繰り返す。横断テーマT-Cと接続。

---

## 6. 横断テーマ（5次元統合レポートとの接続）

5次元統合レポートで整理された5つの横断テーマを、本仕様書の4層上にどう現れるかでサマリする。詳細は5次元統合レポートへ。

### T-A 文面の規律 vs コードの規律

スキル本文・docstring・MCP instructions・tag-notesで規律を表現する設計が強みだが、コードガード・スキーマ制約・ツールシグネチャに降りていない。AIが本文を読み飛ばすと素通りする。本仕様書のplaybook各節（特に5章協調層の記録ガード、4章フロー層のnudge発火原則）が文面規律の典型で、根治には型レベル・APIガードへの降ろしが必要。

### T-B ライフサイクル

retract/supersedes/pending_spawn/「合意→実装」のいずれも全状態遷移の設計が後回し。本仕様書では2.2のretractライフサイクル節、5.2の文脈分断抑制節に現れる。プロトコル層で「論理的に消えた」と宣言するだけでなく、ストア層・フロー層・協調層への伝播を機械的に保証する設計が要る。

### T-C 補完チャネル重複

「思い出させる」目的のチャネルが事後hint / Stop nudge / harness推奨 / coverage / tag-notes と並走し、しきい値・状態管理がバラバラ。本仕様書では4.2のhint/nudge発火原則節がここに直接接続する。HintService単一窓口化が処方箋候補。

### T-D 重複と効果測定不在

decision/log・関係メカニズム5系統・検索SQL3関数など同型コードが拡散し、効果測定の仕組みがないため重複削除やパラメータ調整の判断ができない。本仕様書の2.1エンティティ型（decision/log構造同型）、3.1スコア解釈の節がここに接続する。search_telemetry導入が処方箋候補。

### T-E マルチセッション境界

session_id を捨てる heartbeat、events.jsonl と relay の二系統真実源、orch-managedをタグで抑止、pending_spawn無期限残留など。本仕様書では5章全体がこのテーマと接続する。session_id heartbeat同梱、orch-managed構造的分離が処方箋候補。

---

## 7. 用語集

**4層スタック**: CALM広義を整理する4つの層（プロトコル/ストア/フロー/協調）。プロダクト群マップv1で確定。

**3プロトコル**: トランスポート（封筒）/ 協調（台本）/ 記憶（書庫）の3層プロトコル。本仕様書の対象は「記憶」プロトコル + ストア/フロー/協調層。

**topic**: 1つの関心事・問題・機能。議論の容れ物。

**decision**: 結論。reasonとセットで記録される。supersedesで上書き、retractで論理削除。

**discussion_log（log）**: 議論の経緯・議事録。結論に至る道筋を保存する。

**activity**: 作業の単位。「SVで何をするか表せる」が判断基準。記録の顔（過去形）とタスクの顔（未来形）の2面を持つ。

**material**: 成果物・ドラフト・調査結果。要約せず生データを保存する。双方合意なしに保存可。

**relation**: エンティティ間の双方向の関連。

**supersede**: decision間の上書き関係。後継が前任を置換する。

**retract**: 論理削除。retracted_atを立てる。物理削除は別問題（ストア層責務）。

**pin**: 任意エンティティの強調。長期にわたって参照され続けるものに付ける。

**tag-notes**: タグに紐づく教訓・運用ルール。タグ遭遇時にAIへ自動注入される。

**habits**: 全セッション共通の行動ルール。正はDBで、`trigger_mode='always'`は`~/.claude/rules`配下の自動生成ファイル経由で全文配信、`'intelligently'`はタイトルのみのマニフェスト表示。

**check-in**: アクティビティに紐づく文脈を一括取得する作業開始の入り口。

**nudge**: 記録忘れ防止等の自動システムリマインダー。record nudge / follow_up nudge / recompose hintなど。

**hint**: ツールレスポンスやcheck-in結果に注入される提案。recompose提案などの文脈依存ナッジ。

**scoring**: SessionStartでの未完アクティビティの優先度付け。

**coverage**: check-inで「どれだけ情報を引けたか」を示すメトリクス。

**orch**: orch/workerフレームワークの指揮役。worker spawn・queue管理・decision記録の集約を担う。

**worker**: orch/workerフレームワークの実作業役。実装・テスト・PR等を担当する。

**orch-managed**: orch運用下で生成されたエンティティに付与するタグ。個人フローから除外するシグナル。

**sync-memory**: セッション終了前に transcript を解析し、CALM への一括記録を行うスキル。

**anchor**: 各specの末尾に明記する「今も正しいかどこを見れば検証できるか」の生きた参照先。コードベース/既存決定/既存資料の3種類。sourceとは区別する。

**source**: materialの出自（過去どこから来たか、固定）。anchorとは区別する。

**playbook**: 「こう使われるべき」「こう使わないとどうなる」の行動規範。本仕様書の各章後半。

---

## 残課題（v0 → v1 で詰める）

1. 各specのアンカー欄を、実コードベースのファイル/モジュール名で具体化する（v0では論理名にとどめた）
2. 4.1フロー層spec の scoring判定基準を、SessionStart実装の判定軸と照合して詳細化する
3. 5.1協調層spec の delta通知（実装中）について、実装完了後に振る舞いを書き足す
4. プロトコル層と協調層の接点（worker decision proposalのrelayスキーマ）が薄い。orch側設計書を参照しつつ補強する
5. affordance議論（CALMのデフォルトエクスペリエンス問題）への接続を、4.2playbookと連動して書き足す
6. 横断テーマ章をsummaryのみで5次元統合レポートへリンクする現在の方針が、本仕様書だけ読む読者には情報不足になる可能性。読み口の調整が要る
