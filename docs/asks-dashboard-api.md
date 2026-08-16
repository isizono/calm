# asks ダッシュボード向けHTTP API

## 概要

ask store（人間の判断待ちの問いのインボックス）に対して、MCPプロトコルを話さない外部Webアプリ（ダッシュボード等）からフリーテキストで回答するための、MCPプロトコル外の薄いプレーンHTTP APIである。

- 対象読者: cc-memoryのMCPクライアント（Claude Code等）を経由せず、ブラウザ等から直接askを閲覧・回答したい外部アプリ
- 認証: なし。cc-memoryはlocalhost（`127.0.0.1`）に他者がアクセスできる状況を脅威モデルに含めていないため、本APIも同じ前提を継承する。localhost以外からアクセス可能なネットワーク構成で運用する場合は、リバースプロキシ等で別途アクセス制御を行うこと
- ベースURL: `http://localhost:52837`
- 対象操作: askの一覧取得（`get_asks`相当）と回答（`answer_ask`相当）の2つに限定される。decision作成・トリアージ・取り下げ等、破壊的・機微な操作はこのAPIからは行えない

## Originチェック / CORS

同一マシン上の他者アクセスは上記の脅威モデルの外だが、ブラウザ経由のCSRF（別サイトを閲覧中に本APIへ意図しないリクエストを送らせる攻撃）には低コストで対策できるため、2つのエンドポイントとも共通のOriginヘッダチェックを行う。`Origin`ヘッダが送られてきた場合、その値が`http://localhost`または`http://127.0.0.1`（いずれも任意ポート）で始まらなければ`403`（`{"error": {"code": "FORBIDDEN", "message": "origin not allowed"}}`）で拒否する。`Origin`ヘッダが無いリクエスト（curl・Postman・サーバーサイドスクリプト等）は許可する。CSRFはブラウザが自動付与する`Origin`ヘッダを悪用する攻撃であり、ヘッダの無いリクエストはそもそも攻撃の対象外のため。

ダッシュボードが本APIと別オリジン（別ポート等）で動く構成を想定し、CORSも許可している。許可オリジンはOriginチェックと同じ`http://localhost:*`・`http://127.0.0.1:*`に限定しており、ワイルドカード（`*`）による全許可はしていない。許可メソッドは`GET`/`POST`/`OPTIONS`、許可ヘッダは`Content-Type`のみ、`Access-Control-Allow-Credentials`は付与しない（Cookie等の資格情報を伴うクロスオリジンリクエストは想定しない）。CORS設定はfastmcpの`mcp.run(..., middleware=[...])`経由でStarletteの`CORSMiddleware`をサーバー全体のASGIアプリに適用する形で組み込まれており、`/api/asks`系以外のエンドポイントにも同じlocalhost限定の許可範囲が及ぶ。

## エンドポイント一覧

### GET /api/asks

open状態のask一覧を取得する。

**クエリパラメータ**

| 名前 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- |
| status | no | `open` | `open`/`answered`/`promoted`/`dismissed`/`withdrawn`のいずれか |

**レスポンス（200）**

```json
{
  "asks": [
    {
      "id_raw": 42,
      "question": "案A・案Bどちらで進める？",
      "context": "...",
      "status": "open",
      "kind": "ask",
      "choices": ["案A", "案B"],
      "occurrence_count": 1,
      "first_seen_at": "2026-08-16 09:00:00",
      "last_seen_at": "2026-08-16 09:00:00",
      "blocks": [{"id_raw": 7, "title": "...", "status": "in_progress"}],
      "requesters": ["session-abc"],
      "tags": ["domain:cc-memory"]
    }
  ],
  "total_count": 1
}
```

`choices`はaskにテンプレートが設定されていれば文字列配列、未設定であれば`null`。

**レスポンス（400 / 403 / 500）**

```json
{"error": {"code": "VALIDATION_ERROR", "message": "..."}}
```

`status`が不正な値のときは400（`VALIDATION_ERROR`）、`Origin`ヘッダが許可外のときは403（`FORBIDDEN`、詳細は上記「Originチェック / CORS」参照）、DB起因のエラーは500（`DATABASE_ERROR`）。

### POST /api/asks/{ask_id}/answer

open状態のaskに1回だけ回答する。

**パスパラメータ**

| 名前 | 型 | 説明 |
| --- | --- | --- |
| ask_id | int | 対象askのID |

**リクエストボディ**

```json
{"answer_body": "案Aで進めてください"}
```

`answer_body`は空文字列不可・8000字以内の自由文字列。`choices`で選択肢が提示されていても、このエンドポイントが受け取るのは常にフリーテキストであり、選択肢の値をそのまま送るかどうかはクライアント側の判断に委ねられる。

**レスポンス（200）**

```json
{
  "id": 42,
  "status": "answered",
  "triage_pending": true,
  "blocked_activities": [7],
  "next_step": "triage_askでpromote/dismissへ振り分けてください。"
}
```

**レスポンス（400 / 403 / 500）**

```json
{"error": {"code": "VALIDATION_ERROR", "message": "..."}}
```

`ask_id`が整数でない・リクエストボディが不正なJSON・`answer_body`が文字列でない・対象がopen状態でない、はいずれも400（`VALIDATION_ERROR`）。`Origin`ヘッダが許可外のときは403（`FORBIDDEN`、詳細は上記「Originチェック / CORS」参照）。DB起因のエラーは500（`DATABASE_ERROR`）。

## サンプル

### curl

```bash
# open askを一覧取得
curl http://localhost:52837/api/asks?status=open

# ask id=42 に回答
curl -X POST http://localhost:52837/api/asks/42/answer \
  -H "Content-Type: application/json" \
  -d '{"answer_body": "案Aで進めてください"}'
```

### fetch()

```js
// open askを一覧取得
const asks = await fetch("http://localhost:52837/api/asks?status=open")
  .then((r) => r.json());

// ask id=42 に回答
const result = await fetch("http://localhost:52837/api/asks/42/answer", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ answer_body: "案Aで進めてください" }),
}).then((r) => r.json());
```
