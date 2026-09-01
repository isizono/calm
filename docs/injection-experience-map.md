# 注入体験マップ

## 0. 読み方

本ドキュメントはAIエージェントへの自動注入面（SessionStart hookの各セクション、`check_in`応答、MCP instructions）ごとに、予算（字数上限の目安）と保証種別を一覧化したものである。仕様凍結を目的とせず、注入量の膨張を検知・議論するための作業用ドキュメントとして扱う。

一次情報は各注入面を実装するコード（`hooks/session_start_hook.py`、`src/services/checkin_service.py`、`src/main.py` の `RULES` 定数）であり、本ドキュメントと食い違った場合はコードが正である。

### 保証種別の凡例

- **complete**: 対象データを全件配信する。省略・切り詰めは発生しない
- **selected+remainder**: 上限件数まで選抜して配信し、選抜から漏れた件数を機械算出して明示する。分母（選抜対象の母集団）は選抜規則と独立に定義される
- **truncated+count**: 予算超過時に本文を切り詰め、欠損件数を明示する

## 1. SessionStart hook

| セクション | 予算（字） | 保証種別 | 備考 |
|---|---:|---|---|
| snapshot警告 | 0（異常時のみ全文） | complete | DBデータ異常減少検知時のみ出力。定常予算はゼロで、異常時は予約枠として全文を出す |
| activities（一覧+固定ナビ） | ≤1,410（階層1 400 + 階層2 800 + 決定論レンダリング注記 70 + 固定ナビ 140） | selected+remainder | 階層1（別セッション作業中）は全件表示。階層2（優先）は in_progress かつ7日以内、または pinned（60日decay）の中から上位5件を表示。末尾の固定ナビに未表示件数（母集団=active全件−表示済み件数）とpinned内訳を機械算出して明示する。階層1・2とも0件のときはヘッダ・注記を出さず固定ナビのみ返す |
| habits（投影ファイルの鮮度検証+縮退フォールバック） | 0（fresh時）/ 約80字（stale時、通知1行）/ always全文+件数1行（absent時） | fresh・stale時: complete（注入なし、または1行通知のみ）/ absent時: always=complete, intelligently=truncated+count | 通常配信は`~/.claude/rules/cc-memory-habits.md`への自動生成ファイル（launch時読み込み、本hookとは別の注入面）が担う。本セクションはverify_and_healで当該セッションが投影ファイルを読み込めているかを検証するだけで、fresh時は注入ゼロ、stale時は1行通知のみ。投影ファイルが読めない・未生成・kill switch（`CALM_HABITS_RULES_EXPORT=0`）中に限り、always層全文+intelligently層は件数1行の縮退注入を行う |
| signals | ≤120 | complete | 未トリアージ(status='new')件数をkind内訳付きで1行表示。0件時は非表示 |
| relay inbox | 0〜150 | complete | `CALM_RELAY_SESSION_AWARE`未設定（デフォルトOFF）時は非表示（0、identity解決すら試みない）。ON時、identity未解決またはtoken未設定のときのみ非表示。identity解決できればMonitor監視指示1行を常時表示し、未読>0のときのみ「未読N件 → relay_receiveで消化」行を追加（最大2行） |
| sync_policy | ≤250 | complete | 環境変数 `CALM_SYNC_POLICY` の設定値をそのまま全文配信。未設定時は非表示 |

## 2. check_in応答

| フィールド | 保証種別 | 備考 |
|---|---|---|
| recent_decisions | selected+remainder | 関連topic横断で新しい順に上位15件（`DECISIONS_FULL_LIMIT`）。`coverage.decisions` に "選抜件数/総件数" を明示 |
| materials | selected+remainder | リレーション経由のカタログ形式。`coverage.materials` に総件数を明示 |
| logs | selected+remainder | 最新1件はcontent付き、残りはid+titleのカタログ。`coverage.logs` に総件数を明示 |
| pinned | complete | activity自身とそのタグにpinされた対象を全件content付きで返す |
| tag_notes | complete | セッション内初回遭遇時のタグのみ（`intent:`は毎回）。対象タグのnotesは全文 |
| catalog | complete | `get_map` によるリレーショングラフ（depth 1-2）を全件返す |

## 3. RULES（MCP instructions）

| 項目 | 予算（字） | 保証種別 | 備考 |
|---|---:|---|---|
| RULES全文 | ≤2,048（安全マージン1,900、現状は超過中） | complete | MCPクライアント側で2,048字を超えると切り詰められる実態があるため、全文がこのハードリミット以内に収まることを回帰テストで保証する。安全マージンとして1,900字以内を目指すが、確定済み運用ポリシー文言の追加により現状は超過している（既知の超過としてxfailで追跡）。先頭にコンテキスト取得原則・アクティビティ運用・記録の使い分け・タグ運用を置き、末尾に `calm:man` skillへの導線1行を置く |
