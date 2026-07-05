# relay_sdk vendoring 情報

このディレクトリは relay リポジトリの `relay_sdk/` パッケージを vendoring したものである。

## 出自

- リポジトリ: git@github.com:isizono/relay.git（ローカル: ~/workspace/relay）
- コミット: `7bfe4ec`（main、full: `7bfe4ecf57fea9f50cd58cf1ccb2daf6e49e1d92`）
- コピー日: 2026-07-05

## 改変内容

コピー元との差分は import 文の書き換えのみ（`from relay_sdk...` → `from src.relay_sdk...`）。
コードの挙動・ロジックは一切改変していない。logger 名（`relay_sdk.*`）と docstring 内の
モジュールパス表記は出自のまま残している。

なお、この配置では outbox dispatcher の CLI 起動は `python -m src.relay_sdk.outbox` になる
（出自の docstring は `python -m relay_sdk.outbox` と記載）。

## 実行時依存

- モジュールレベルの標準ライブラリ外依存は `httpx` のみ（pyproject.toml に追加済み）。
- `http/auth.py` の JWS 系関数（`sign_jws` / `verify_relay_agent_card`）は関数内で
  `jwt` / `rfc8785` / `joserfc` を遅延 import する。これらは `jws_key_path` を指定した
  場合のみ必要になるため依存には追加していない。静的 Bearer token 運用では使われない。

## 再同期手順

relay リポジトリ側で SDK が更新された場合、以下で再同期する。

```bash
# 1. コピー元の対象コミットを確認・記録する
git -C ~/workspace/relay log --oneline -1

# 2. パッケージ全体を上書きコピー（__pycache__ 除外）
rsync -a --delete --exclude='__pycache__' --exclude='VENDORED.md' \
  ~/workspace/relay/relay_sdk/ src/relay_sdk/

# 3. import を機械的に書き換え
find src/relay_sdk -name '*.py' -exec sed -i '' 's/^from relay_sdk/from src.relay_sdk/' {} +

# 4. 差分が import 書き換えのみであることを確認
diff -r --exclude=__pycache__ --exclude=VENDORED.md ~/workspace/relay/relay_sdk src/relay_sdk

# 5. 本ファイルのコミット hash とコピー日を更新し、依存変化があれば pyproject.toml / uv.lock を更新
```

## vendoring の理由

relay リポジトリの pyproject.toml は build-system 未定義かつトップレベルに複数パッケージが
混在しており、path/git 依存としての install がそのままでは失敗する。また cc-memory の CI は
`uv sync --frozen` のためリポジトリ外 path 依存は成立しない。リポジトリ内 vendoring の
前例（src/relay/）に従った。
