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
| habits | 未設定（無制限） | always: complete / intelligently manifest: complete（タイトルのみ） | trigger_mode='always' は本文全文、'intelligently' はタイトルのみのマニフェスト。マニフェスト自体の件数上限は本ドキュメント時点で未実装 |
| signals | ≤120 | complete | 未トリアージ(status='new')件数をkind内訳付きで1行表示。0件時は非表示 |
| relay inbox | 0〜150 | complete | 未読0件は非表示。未読>0のときのみ「未読N件 → relay_receiveで消化」+ Monitor監視指示の2行以内 |
| sync_policy | ≤250 | complete | 環境変数 `CCM_SYNC_POLICY` の設定値をそのまま全文配信。未設定時は非表示 |

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
| RULES全文 | ≤1,900 | complete | MCPクライアント側で2,048字を超えると切り詰められる実態があるため、全文が収まる分量に収める。先頭にコンテキスト取得原則・アクティビティ運用・記録の使い分け・タグ運用を置き、末尾に `cc-memory:guide` skillへの導線1行を置く |
