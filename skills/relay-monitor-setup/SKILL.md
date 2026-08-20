---
name: relay-monitor-setup
description: relay session-aware Monitor機構（CALM_RELAY_SESSION_AWARE）を、Claude自身が段階を代行してセットアップ完了まで連れていくオンボーディングウィザード。「relay-monitor-setup」「relay動かして」「relay session-aware使いたい」「relay inboxの通知が来ない」「CALM_RELAY_SESSION_AWAREって何」「前のマシンでは動いてたのに新しいマシンでrelayが動かない」「relay monitorセットアップして」などで発動する。既に環境変数が有効・token取得済み・identity解決も成功しており、hookが実際にrelay監視nudgeを出せている（正常稼働中）状態では発動しない。手順を提示するだけで終わらず、承認が要る変更は対話確認のうえ実際に実行し、動作確認まで完了させる。
---

# relay-monitor-setup

relay session-aware Monitor機構は、CALMをインストールしただけでは動かない。環境変数・credential・identity解決・relayサーバー稼働のすべてが揃って初めて機能する多段構成であり、1段でも欠けると各段はfail-open（例外を出さず黙って何も注入しない）で沈黙するため、ユーザー自身は「なぜ動かないか」に気づけない。

本スキルは、この前提知識をユーザーが一切持っていないことを前提に、Claudeが環境をスキャンし、直せる部分は対話確認のうえ実際に直し、疎通確認までやり切る。「手順を提示して終わり」のトラブルシューティングツールではない。

## スコープ

対象は **ローカルのrelay session-aware Monitorセットアップ** のみ。

- 環境変数設定（`CALM_RELAY_SESSION_AWARE`）
- credential.json取得（招待URL redeem）
- identity解決の確認
- relayサーバーの稼働確認・（登録済みなら）再起動
- cc-memoryローカルMCPサーバーの疎通テスト

**対象外**:

- リモートMCPサーバーのセットアップ（Cloudflare Tunnel等）。将来このスキルに統合される可能性はあるが、現時点では範囲外
- relayサーバーの新規導入（`relay`リポジトリのclone、launchd常駐化の初回構築）。既に登録済みのサーバーの再起動は行うが、ゼロから常駐サービスを構築する操作はガイド提示のみに留める（`docs/ops/relay-server.md`参照）

## 前提知識（コード裏取り済み）

- `CALM_RELAY_SESSION_AWARE`が現行の環境変数名（`src/config.py`）。旧名`CCM_RELAY_SESSION_AWARE` / `CC_MEMORY_RELAY_SESSION_AWARE`も`src/env_compat.py`のフォールバックにより等価に効くが、新規に設定する場合は現行名を使う
- このフラグが効くのは **3つのhookのみ**（`hooks/session_start_hook.py` / `hooks/user_prompt_submit_hook.py` / `hooks/relay_monitor_watch_hook.py`）。`relay_post` / `relay_publish` / `relay_subscribe` / `relay_receive`等のMCP toolやRelayRuntime本体はこのフラグを一切見ない（token（credential.json等）さえ揃っていれば、このフラグがOFFでも手動呼び出しは動く）。フラグの役割は「hookがMonitor監視を自動で案内するか」だけ
- token解決順は env `RELAY_BEARER_TOKEN` → `credential.json`（`RELAY_STATE_DIR`、既定`~/.cc-memory/relay`） → なし。いずれも無いとrelay機能全体が`config_missing`で縮退する
- credential.json取得（招待URL redeem）は関連リポジトリ`relay`側での招待発行（`python -m relay.invite new --identity cc-memory`）が前提。**この発行操作はcc-memoryリポジトリに属さない**ため、`relay`リポジトリがローカルに無い環境では自動化できない
- identity解決（`resolve_identity_by_ancestry()`）は、自プロセスの祖先pidチェーン直近2ホップと、`~/.cc-memory/relay/sessions/launcher-*.json`に登録された各launcherの祖先pidチェーン直近2ホップの交差で判定する（fail-close、推定はしない）。hookとは別プロセス経由の呼び出しでも、双方が同じClaude Code CLIプロセスの子孫である限り正しく解決できることを実機で確認済み
- `relay_status` MCP toolは`configured`（token有無）/`running`（RelayRuntime起動有無）は返すが、relayサーバーへのHTTP到達性・`CALM_RELAY_SESSION_AWARE`の状態・identity解決可否のいずれも返さない。本スキルが新たにこれらを機械的にチェックする

## 発動判定

以下をすべて満たす場合は発動しない（既に正常稼働中）。

1. `~/.claude/settings.json`の`env`に`CALM_RELAY_SESSION_AWARE` / `CCM_RELAY_SESSION_AWARE` / `CC_MEMORY_RELAY_SESSION_AWARE`のいずれかが`"1"`で設定されている
2. token（`RELAY_BEARER_TOKEN`環境変数、または`credential.json`）が解決できる
3. 今の会話のSessionStartコンテキストに注入された`transcript path`から導出したsession_idについて、`~/.claude/.claude-code-memory/state/relay_identity_<session_id>`が存在する（実際にこのセッションでhookがidentity解決に成功した証跡）

1つでも欠ける場合は発動する。3番目の判定はセッション開始直後でまだUserPromptSubmit hookが1回も走っていない場合に「未判定」になりうるため、その場合は1・2のみで判断し、identity解決はStep 1で実測する。

## Step 1: 環境スキャン（読み取りのみ）

この段階では何も変更しない。

### 1-0. プラグイン実体パスの特定

hookも MCP serverも `${CLAUDE_PLUGIN_ROOT}` 配下のコードで動く。これはgitチェックアウト（`~/workspace/cc-memory`等）とは限らず、marketplace cache配下（`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`）のことが多い。**診断は実際に動いているコードに対して行う** ため、まずこれを特定する。

```bash
find ~/.claude/plugins -maxdepth 8 -type f -path '*/services/relay/identity.py' -exec stat -f '%m %N' {} \; 2>/dev/null | sort -rn
```

最終更新（`%m`）が最も新しい行のパスから`src/services/relay/identity.py`を除いた部分をプラグインルート（以下`$PLUGIN_ROOT`）とする。複数ヒットするのは古い marketplace 登録が残っている環境（例: リポジトリ名変更の移行途中）で、直近に同期されたものが実際に使われている可能性が高いための tiebreaker。見つからない場合はプラグイン自体が正しくインストールされていない可能性が高く、このスキルの対象外（プラグイン導入自体の問題）としてユーザーに報告する。

### 1-1. 環境変数

```bash
python3 -c "
import json
d = json.load(open('$HOME/.claude/settings.json'))
env = d.get('env', {})
for name in ('CALM_RELAY_SESSION_AWARE', 'CCM_RELAY_SESSION_AWARE', 'CC_MEMORY_RELAY_SESSION_AWARE'):
    print(name, '=', env.get(name))
"
```

現行名・旧名のいずれかが`"1"`なら設定済み。すべて未設定またはすべて`"1"`以外なら未設定。

### 1-2. credential.json

```bash
STATE_DIR="${RELAY_STATE_DIR:-$HOME/.cc-memory/relay}"
ls -la "$STATE_DIR/credential.json" 2>&1
```

存在すれば、権限が`0600`か、パースできてidentity/base_url/bearer_tokenの3キーが揃っているかを確認する（`bearer_token`の値自体は絶対に出力・記録しない）。

```bash
python3 -c "
import json
d = json.load(open('$STATE_DIR/credential.json'))
print('identity:', d.get('identity'))
print('base_url:', d.get('base_url'))
print('has_bearer_token:', bool(d.get('bearer_token')))
print('expires_at:', d.get('expires_at'))
"
```

env `RELAY_BEARER_TOKEN`が設定されている場合はbreak-glass経路で優先されるため、credential.json不在でも「未構成」と即断しない。

### 1-3. relayサーバー到達性

```bash
BASE_URL="${RELAY_BASE_URL:-http://localhost:8770}"
curl -sS -m 3 -o /dev/null -w "http_code=%{http_code}\n" "$BASE_URL/"
```

`credential.json`があれば`base_url`をそちらから優先して使う。到達不可（curl exit非0、または`http_code=000`）ならサーバー未起動または未導入。

### 1-4. relayサーバーのlaunchd登録状態（到達不可だった場合のみ）

```bash
launchctl print gui/$(id -u)/com.isizono.relay-v2 2>&1 | grep -E '^(gui|\s*state)'
```

`state = running`が出れば登録済み・稼働中（1-3の到達不可判定と矛盾する場合はport不一致等の別要因を疑う）。`state =`行自体が無い、またはコマンドが`Could not find service`等のエラーを返す場合は未登録＝「初回導入が必要（ガイドのみ）」、`state`が`running`以外（`not running`等）なら「登録済みだが停止中（軽量に自動修復可能）」に分類する。

### 1-5. relay_status（MCP tool）

`relay_status()`を呼び、`runtime.configured` / `runtime.running`を確認する。`configured=false`ならtoken未解決、`running=false`ならこのプロセスでRelayRuntimeが起動していない（stdio transport・remoteプロセス・token未設定のいずれか）。

### 1-6. identity解決の実測

```bash
cd "$PLUGIN_ROOT" && uv run python -c "
from src.services.relay.identity import resolve_identity_by_ancestry
print(resolve_identity_by_ancestry())
"
```

`resolve_identity_by_ancestry()`は今このBashコマンドを実行しているプロセス自身の祖先pidチェーンで判定する。hookとは別プロセス経由だが、どちらも同じClaude Code CLIプロセスの子孫であれば同じ結果に収束する（実機検証済み）。`None`以外（UUID文字列）が返れば解決成功。

`None`が返った場合、登録済みlauncherの生存状況を追加で確認する。

```bash
ls "$STATE_DIR/sessions/" 2>&1
```

0件なら「生きているlauncher登録が無い」（現在動いているClaude Code CLIセッションが1つも無いか、登録直後で未反映）。1件以上あるのに`None`が返るなら、祖先チェーンが窓（直近2ホップ）の外にある特殊な起動経路（ネストしたsubagent環境等）の可能性がある。

### 1-7. hookの実測証跡（このセッション自身の検証）

今の会話のSessionStartコンテキストに「このセッションのtranscript path: `<path>`」という行があれば、`<path>`のbasenameから拡張子を除いたものがこのセッションの`session_id`である。

```bash
SESSION_ID="<導出したsession_id>"
cat "$HOME/.claude/.claude-code-memory/state/relay_identity_${SESSION_ID}" 2>&1
```

存在すれば、実際にhookがこのセッションでidentity解決に成功した直接証拠（1-6の実測より強い証拠）。無くても1-1の環境変数が未設定なら想定通り（hookがそもそも解決を試みていない）なので異常ではない。

## Step 2: 結果分類

| 項目 | OK | 警告 | 要修復 |
|---|---|---|---|
| プラグイン実体パス | 特定できた | - | 特定できない（プラグイン導入自体の問題） |
| 環境変数 | 現行名で`"1"` | 旧名（`CCM_`/`CC_MEMORY_`）で`"1"` | 未設定 |
| credential.json | 存在・0600・3キー揃い | 権限が0600以外 | 不在かつ`RELAY_BEARER_TOKEN`も未設定 |
| relayサーバー到達性 | `http_code`が2xx | - | 到達不可 |
| relayサーバー登録状態 | - | 登録済みだが停止中 | launchd未登録（初回導入要） |
| relay_status | `configured=true`かつ`running=true` | `configured=true`かつ`running=false`（stdio/remoteプロセスの可能性） | `configured=false` |
| identity解決 | UUIDが返る | - | `None`（生きているlauncher登録があるのに解決失敗） |
| hook実測証跡 | マーカーファイルあり | 環境変数未設定のため未判定（想定通り） | 環境変数`"1"`かつtoken有りなのにマーカー無し |

## Step 3: 修復

### 軽量副作用（自動実行、承認不要）

- `mkdir -p ~/.cc-memory/relay`（state dir自体は各コードが遅延生成するが、後続チェックのため先に用意しておく。既存ファイルは一切変更しない）

### 承認要（変更前に内容を提示し、承認後に実行）

**a. 環境変数の追加**

`~/.claude/settings.json`の`env`キーのみを対象に、`jq`で該当キーだけ差し込む（全体書き換えはしない）。

```bash
jq '.env.CALM_RELAY_SESSION_AWARE = "1"' ~/.claude/settings.json > /tmp/settings.json.new \
  && diff ~/.claude/settings.json /tmp/settings.json.new
```

diffをユーザーに提示し、承認後に

```bash
mv /tmp/settings.json.new ~/.claude/settings.json
```

**この変更は今のセッションには反映されない。** hookは新規プロセスとして毎回spawnされるが、Claude Code CLIが子プロセスに渡すenvはCLI自身の起動時点のものであるため、設定ファイルを書き換えても今のセッションのhook呼び出しには乗らない。**Claude Codeセッションの再起動が必要**であることを明示し、Step 5で次のアクションとして案内する。

**b. credential.json取得（招待URL redeem）**

**実行条件（これを満たさない限りcredentialを新規発行しない）**: `get_token()`が`None`（credential.json不在かつ`RELAY_BEARER_TOKEN`も未設定）。既存のcredential.jsonがある場合は、それが動いていないと判明しない限り（後述の再発行判断）新規発行しない。`docs/ops/relay-server.md`の再発行Aの通り、新規発行しても旧credentialは失効しないため、動いているcredentialがあるのに理由なく発行すると無期限に生き続ける孤児bearerが増える。

まず`relay`リポジトリのローカル所在を探す。

```bash
for cand in ~/workspace/relay ~/repos/relay ~/relay; do
  test -d "$cand/.git" && echo "found: $cand"
done
```

- **見つかった場合**: 招待URLはfragment（`#v=1&t=...`）にsecretを含むため、コマンド文字列に生の値を書かない。シェル変数越しに1つのBashコマンドで完結させる。

  ```bash
  INVITE_URL=$(cd <relayリポパス> && python -m relay.invite new --identity cc-memory | grep -o 'http[^ ]*#v=1&t=[^ ]*')
  cd "$PLUGIN_ROOT" && printf '%s\n' "$INVITE_URL" | uv run python -m src.services.relay.redeem
  ```

  実行前にユーザーへ「新しいrelay credentialを発行してcc-memory側で受け取ります」と一言確認してから実行する。

- **見つからなかった場合**: 自動化できない。ユーザーに次のいずれかを依頼する。
  1. `relay`リポジトリを持つ別のマシン・別のセッションで`python -m relay.invite new --identity cc-memory`を実行してもらい、出力された招待URLをこの会話に貼ってもらう
  2. 貼られた招待URLを受け取ったら、redeemの実行はClaudeが代行する（`printf '%s\n' "<貼られたURL>" | uv run python -m src.services.relay.redeem`、`$PLUGIN_ROOT`で実行）

  招待URLは15分・1回限りで失効するため、受け取ったら即座にredeemする。

**c. relayサーバーの再起動（launchd登録済みだが停止中の場合のみ）**

```bash
launchctl kickstart -k gui/$(id -u)/com.isizono.relay-v2
```

実行前に「登録済みのrelayサーバーを再起動します」と確認する。launchd未登録（初回導入）の場合はここに進まず、「提案のみ」章へ回す。

**d. cc-memoryローカルMCPサーバーの再起動**

credential.json取得後や環境変数変更後、実行中のローカルサーバー（port 52837）にtoken解決結果を反映させるには再起動が要る。

```bash
lsof -ti tcp:52837 -sTCP:LISTEN | xargs kill; uv run python -m src.launcher &
```

**生存中の全セッションの接続が一時的に切れる**操作である旨を明示してから承認を得る。実行後、生存セッションは`/mcp`からreconnectが必要。

修復のたびにStep 1の該当チェックを再実行して結果を確定する。外部インストール待ちなど修復不能なブロッカーが残ったら、その時点で報告して以降のステップをスキップする。

### 提案のみ（Claudeは実行しない）

- `relay`リポジトリの新規clone（`git clone git@github.com:isizono/relay.git ~/workspace/relay`）。private repoへのアクセスが要るため
- relayサーバーの初回launchd常駐化。`docs/ops/relay-server.md`の「macOS launchdで常駐化する例」を提示し、コマンドの実行はユーザーに委ねる
- credential漏洩時の再発行B（revoke + relay再起動 + 再発行）。稼働中クライアント全員を巻き込む破壊的操作のため、コマンド一式を提示するに留め実行しない

## Step 4: 疎通テスト

Step 3で環境変数を新規追加した場合、**このセッション内では反映されないため疎通テストは完走できない**。その場合はStep 5に進み、セッション再起動後の再実行を案内する。

環境変数が既に有効なセッションでのみ、以下を実施する。`relay_publish`はlabelsルーティング方式で、identityを直接指定する送り先指定は無い。自分の`handle:`labelを購読したうえで同じlabelにpublishすることで、自分の受信経路を実際にエンドツーエンドで確認する（実機で動作確認済みの手順）。

1. `relay_status()`を呼び、`runtime.configured=true`かつ`runtime.running=true`を確認
2. `relay_subscribe(labels=[])`を呼ぶ。空配列は「自分のhandle宛（直接メッセージ）のみを購読」を意味する。応答の`identity`（このセッションのrelay identity）と`handle`（例: `session-<8桁>`）を控える
3. 控えた`identity`でinbox pathを組み立て、`Monitor`ツールで`persistent: true`で監視を開始する

   ```
   Monitor(command="tail -f ~/.cc-memory/relay/inbox/session-<identity>.jsonl", description="relay inbox疎通テスト", persistent=true)
   ```

4. `~/.claude/.claude-code-memory/state/monitor_started_<session_id>`が作成されたことを確認する（PostToolUse hookがMonitor呼び出しを検知した証跡）
5. `relay_publish(labels=["handle:<控えたhandle>"], body="疎通テスト")`で自分宛にテストメッセージを送る
6. `relay_receive(peek=true)`で該当メッセージを回収できること、Monitorの通知として届くことを確認する。確認できたら`relay_receive(peek=false)`で既読化してテストメッセージを消化する

すべて成功すれば疎通OK。3または4で失敗する場合は、`tool_input.persistent`が`true`になっているか、inbox pathが一致しているかを再確認する（`hooks/relay_monitor_watch_hook.py`はpathの部分一致でしか判定しないため、pathの取り違えが典型的な失敗要因）。5・6が届かない場合はStep 1-6のidentity解決結果と、2で取得した`identity`が一致しているか（同一セッション内で複数の識別子を混同していないか）を確認する。

疎通テストで開始したMonitorはテスト後もそのまま残してよい（本来のセッション中監視として機能する）。不要なら`TaskStop`で終了できる。

## Step 5: 完了報告

以下をMarkdownで報告する。

- Step 1全チェック結果一覧（OK / 警告 / 要修復）
- 修復した項目、しなかった項目（承認拒否・外部リポジトリ不在などの理由込み）
- 疎通テスト結果
- 次のステップ案内:
  - 環境変数を新規追加した場合: 「Claude Codeセッションを再起動してから、もう一度このスキルを実行すると検証が完了します」
  - すべて疎通確認まで完了した場合: 「以降、relay inboxに新着があるとMonitor経由で通知されます。設定は完了です」

## トラブルシューティング

詳細な障害パターンと復旧手順は `docs/ops/relay-server.md` の「トラブルシューティング」章が正本。本スキルのStep 1-6実測の結果と合わせて典型例を挙げる。

- **環境変数`"1"`・token有りなのにhookのnudgeが出ない**: セッション再起動を挟んでいない可能性が高い（`~/.claude/settings.json`の変更は次回起動時のみ反映）
- **credential.jsonはあるのに`relay_status`が`configured=false`**: `RELAY_STATE_DIR`の参照先がredeem時と実行時でずれている（env上書きの有無を確認）
- **credential.jsonも`relay_status`もOKなのにidentity解決が`None`**: `~/.cc-memory/relay/sessions/`にこのセッションのlauncher登録が無い（launcher起動直後で未反映、またはlauncherプロセス自体が別の起動経路を通っている）
- **identity解決はOKなのにMonitor起動後もマーカーが立たない**: `Monitor`呼び出し時に`persistent: true`を渡し忘れている、またはinbox pathの文字列が完全一致していない
- **redeemに成功した（`credential.json`は存在する）のに`relay_status`が`configured=false`のまま**: cc-memoryローカルサーバー（port 52837）の再起動が必要（`RelayRuntime`の起動判定はプロセス起動時に一度だけ評価される）
