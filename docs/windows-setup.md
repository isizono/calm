# Windows 11 セットアップ手順

対象はWindows 11（x64・ARM64）、Windows PowerShell 5.1とPowerShell 7のどちらでも動くように書く（`curl`ではなく`curl.exe`/`Invoke-RestMethod`、`&&`を使わない等）。

前提として、Claude Code（ネイティブ版）がインストール済みでログイン済みであること。Git for Windowsは無くてもよい（入っている場合、Claude Codeがスキルの中のシェル手順をBashツール経由＝Git Bashで実行するようになる。無ければPowerShellツールで実行される。calmのhookはexec formで登録されておりシェルを経由しないため、hook自体の実行はGit for Windowsの有無に影響されない）。

2つの手順を用意する。

- [パターン1](#パターン1-人がpowershellで実行する手順): 人がPowerShellで1行ずつ実行する手順
- [パターン2](#パターン2-claude-codeに貼り付けるプロンプト): Claude Codeに貼り付けて、確認・実行・成功確認を自走させるプロンプト

状態確認・停止・lockの後始末、社内プロキシ環境での注意は[README.mdのWindows 11の節](../README.md#windows-11での利用)に既にあるため、本ドキュメントでは重複させずリンクする。

## パターン1: 人がPowerShellで実行する手順

### 1. アーキテクチャの確認

以降の手順（特にARM64向けのx64 Python導入）で分岐するため、先に確かめる。

```powershell
$env:PROCESSOR_ARCHITECTURE
```

成功の目印: `AMD64`（x64機）または`ARM64`が返る。

### 2. uvの導入とPATHの反映

公式インストーラでインストールする。

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

インストール直後は現在のPowerShellウィンドウにPATHが反映されない。**新しいPowerShellウィンドウを開き直す**（最も確実）か、現在のウィンドウのままなら以下でユーザー環境変数のPATHを読み直す。

```powershell
$env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
```

確認:

```powershell
uv --version
```

成功の目印: バージョン番号が表示される。

### 3. Visual C++再頒布可能パッケージ（x64）の導入

CALMが依存するtorchのimportに必要。無いと`c10.dll`のロードに失敗しWinError 126になる（実機のWindows 11 ARM64 VMで実際に発生させて確認済み）。ARM64機でも、x64エミュレーション下で動くtorchである以上この再頒布可能パッケージが要る。

```powershell
winget install -e --id Microsoft.VCRedist.2015+.x64 --accept-source-agreements --accept-package-agreements --silent
```

このコマンドはVM上で実行し、インストール後に`import torch`が成功することを確認済み。`--silent`はインストーラ自体のUIを抑止するだけで、管理者権限への昇格（UAC）は別に発生しうる。UACの確認ダイアログが出たら許可する（ただしVMではSSH経由の管理者アカウントで実行したため、対話的なUACダイアログが実際に出るかどうかまでは確認できていない）。

winget自体が無い環境では、[Microsoft公式の配布ページ](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist)からx64版のインストーラを手動でダウンロードして実行する。

成功の目印: wingetがインストール成功を返す。実際に効いているかの最終確認は、6.の`uv sync`後にtorchが読み込めること（`/mcp`がConnectedになること）で行う。

### 4. ARM64の場合だけ: x64のCPythonとUV_PYTHON

ARM64版Windowsでは、CALMの依存（torch・sqlite-vec・cryptography）に`win_arm64`向けのwheelが無いため、uvが既定で選ぶARM64版のCPythonでは動かない。x64版のCPythonをuvで入れ、x64エミュレーション（Prism）上で動かす。

```powershell
uv python install cpython-3.12-windows-x86_64-none
```

このコマンドと、x64 CPython上でtorch等が実際にimportできることはVM上で確認済み（`uv add torch sqlite-vec cryptography`した別プロジェクトで検証）。

次に、`uv sync`を実行するシェルだけでなく、Claude Codeが起動する`uv`からも見える必要があるため、ユーザー環境変数として永続化する。

```powershell
[System.Environment]::SetEnvironmentVariable('UV_PYTHON', 'cpython-3.12-windows-x86_64-none', 'User')
```

この`SetEnvironmentVariable`呼び出し自体（UV_PYTHON個別）はVMで未確認。README.mdの社内プロキシ環境変数の永続化で使っている同じ書式を踏襲した。

設定後は新しいPowerShellウィンドウを開き直してから確認する。

```powershell
$env:UV_PYTHON
```

成功の目印: `cpython-3.12-windows-x86_64-none`が返る。

### 5. マーケットプレイスの追加とプラグインのインストール

既に追加済みかを確認する。

```powershell
claude plugin marketplace list
```

一覧に`calm-marketplace`が無ければ追加する。

```powershell
claude plugin marketplace add isizono/calm
```

既にある場合、最新化したいときだけ以下を実行する。

```powershell
claude plugin marketplace update calm-marketplace
```

プラグインをインストールする。

```powershell
claude plugin install calm@calm-marketplace
```

既にインストール済みで更新したいときは以下を実行する。

```powershell
claude plugin update calm@calm-marketplace
```

成功の目印: インストール（または更新）の成功メッセージが表示される。`claude plugin list`でも`calm@calm-marketplace`が確認できる。

この節のコマンド群（`marketplace list`・`update`・`plugin update`の具体的な出力）はVMで未確認。

### 6. プラグインのディレクトリを特定し`uv sync --frozen`

初回の依存取得（`uv sync`）は1分以上かかることがあり、Claude CodeのMCP接続待ち（30秒）を超えて`/mcp`の初回接続が失敗することがある。プラグインを入れた直後に、プラグインのディレクトリで`uv sync --frozen`を一度手動実行しておく。

ディレクトリは`%USERPROFILE%\.claude\plugins\cache\calm-marketplace\calm\<版>\`の形をしている。`<版>`の部分は`installed_plugins.json`から特定する。

```powershell
(Get-Content -Raw "$env:USERPROFILE\.claude\plugins\installed_plugins.json" | ConvertFrom-Json).plugins.'calm@calm-marketplace'[0].installPath
```

または`Get-ChildItem`で直接見る。

```powershell
Get-ChildItem "$env:USERPROFILE\.claude\plugins\cache\calm-marketplace\calm"
```

特定したディレクトリに移動して実行する。

```powershell
cd "$env:USERPROFILE\.claude\plugins\cache\calm-marketplace\calm\<上で特定した版>"
uv sync --frozen
```

この2通りの特定方法はいずれもVMで未確認（`installed_plugins.json`の構造自体はmacOS版で実機確認済みだが、Windows側でのパス区切り表記は未確認）。

成功の目印: エラー無く完了し、同ディレクトリに`.venv`が作成される。

### 7. `claude mcp list`で接続を確認

```powershell
claude mcp list
```

成功の目印: `plugin:calm:calm`の行が`Connected`と表示される。

### 8. Claude Codeを起動し直し、`/mcp`とフックの動作を確かめる

新しいPowerShellウィンドウで`claude`を起動し直す。

1. `/mcp`を実行し、calmサーバーがconnected状態であることを確認する
2. `/man`を実行し、使い方の案内が返ってくることを確認する（MCPツール呼び出しの疎通確認を兼ねる）
3. `Test-Path "$env:USERPROFILE\.claude\rules\cc-memory-habits.md"`で、SessionStart hookが動いてファイルが生成されていることを確認する

これらはREADME.mdの[正常に動いているかの確認](../README.md#正常に動いているかの確認)と同じ内容である。

## うまくいかないとき

- **MCPの接続ログ**: `%LOCALAPPDATA%\claude-cli-nodejs\Cache\<cwd>\mcp-logs-plugin-calm-calm\`にClaude Code側のMCP接続ログが残る。このパスはWindows実機では未確認（macOS版の`~/Library/Caches/claude-cli-nodejs/`に対応する場所からの類推）。
- **CALMのサーバーログ**: 既定のDBパス（`%USERPROFILE%\.claude\.claude-code-memory\`）と同階層の`logs\server.log`（Pythonの`logging`経由の通常ログ。起動確認・DB初期化等）と`logs\server.stderr.log`（起動直後に落ちるような致命的失敗用。launcherはサーバープロセスの標準出力をDEVNULLに捨てるため、標準出力自体にはログは出ない）に書かれる（`src/main.py`の`_setup_server_logging`、`src/launcher.py`の`_server_stderr_log_path`で確認済み）。`CALM_DB_PATH`を変更している場合はそのディレクトリ配下になる。
- **embeddingサーバーのログ**: `%USERPROFILE%\.cache\cc-memory\embedding-server.log`（`src/infra/embedding_server.py`の`_setup_logging`で確認済み）。
- **残ったlockファイルの扱い**: README.mdの[状態確認・停止・lockの後始末](../README.md#windows-11での利用)を参照。
- **社内プロキシ環境での注意**: README.mdの[社内プロキシ環境での注意](../README.md#windows-11での利用)を参照。
- **初回の`uv sync`を忘れたときの症状**: 初回の`/mcp`接続がタイムアウトする。6.の`uv sync --frozen`を手動実行してから`/mcp`を再接続する。

## パターン2: Claude Codeに貼り付けるプロンプト

前提はパターン1と同じ（Claude Code ネイティブ版、ログイン済み）。PowerShellでClaude Codeを起動し、以下のプロンプトを丸ごと貼り付ける。プロンプトは秘密情報（トークン等）を扱わない。

```
CALM（isizono/calm）をこのWindows機にセットアップしてほしい。各手順は「確認 → 必要なら実行 → 成功の確認」の順で進め、失敗したら次の手順には進まず、何が起きたかと必要な対処を私に伝えて止まってほしい。秘密情報（トークン等）の入力や送信は不要なので扱わないこと。

0. 実行環境の確認
   まず自分が使えるツールがBashツール（Git Bash経由）かPowerShellツールかを確認する。PowerShellツールがあるなら、以降のPowerShellコマンドはそのまま渡して実行してよい。
   Bashツールしか無い場合、PowerShellコマンドを `powershell -Command "..."` のように二重引用符でそのまま埋め込まない。Bash自身が`$env:...`のような`$`始まりの部分を実行前に展開してしまい、コマンドが壊れる。代わりに、実行したいPowerShellコードを一度ファイルに書き出してから `-File` で渡す。ファイルに書き出すときは、ヒアドキュメントの終端記号をクォートしてBashの変数展開を止める（例: `cat <<'PS1' > /tmp/step.ps1` のように終端を `'PS1'` とクォートする）。実行は `powershell -NoProfile -ExecutionPolicy Bypass -File /tmp/step.ps1` のように行う。

1. アーキテクチャの確認
   `$env:PROCESSOR_ARCHITECTURE` を確認し、x64（AMD64）かARM64かを把握する。以降の手順3はARM64のときだけ実行する。

2. uvの導入とパスの扱い
   `uv --version` を試す。失敗する（コマンドが無い）場合のみ、公式インストーラ `irm https://astral.sh/uv/install.ps1 | iex` を実行する。
   自分（Claude）のツール呼び出しは毎回新しいプロセスで動くが、その環境変数はClaude Code本体プロセスが起動した時点のものを引き継ぐ。そのためインストーラがレジストリのユーザー環境変数PATHを更新しても、自分の以降のコマンドにはその更新が自動では反映されない（Windowsの環境変数伝播の一般的な仕組みによる。Claude Code自身のツール実行の内部実装はVMで検証していない）。
   よって以降uvを呼ぶときは、素の`uv`ではなく次のいずれかを使う。
   - 公式インストーラの既定インストール先であるフルパス `$env:USERPROFILE\.local\bin\uv.exe` を直接呼ぶ（このパス自体はVM未確認。無ければ下のPATH再読み込みを使う）
   - 呼び出すスクリプトの先頭で毎回 `$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')` を実行し、レジストリから直接読み直してから`uv`を呼ぶ
   どちらかで `uv --version` が通ることを確認してから次へ進む。

3. （ARM64のときだけ）x64のCPythonとUV_PYTHON
   `uv python install cpython-3.12-windows-x86_64-none` を実行する（手順2で確定させたuvの呼び方を使う）。続けて `[Environment]::SetEnvironmentVariable('UV_PYTHON', 'cpython-3.12-windows-x86_64-none', 'User')` でユーザー環境変数として永続化する。
   これも手順2と同じ理由で自分の以降のコマンドには自動反映されないため、以降uvを呼ぶスクリプトでは毎回 `$env:UV_PYTHON = 'cpython-3.12-windows-x86_64-none'` をその場で明示的にセットしてから呼ぶ。

4. Visual C++再頒布可能パッケージ（x64）
   これは管理者権限とUACの確認が必要なので、自分では実行せず、次のコマンドを提示して人間に実行してもらうよう頼む。実行後、torchが読み込めるかは後の手順（手順7）で間接的に確認できるので、ここでは人間に実行を依頼して完了の報告を待つだけでよい。

   winget install -e --id Microsoft.VCRedist.2015+.x64 --accept-source-agreements --accept-package-agreements --silent

   wingetが無い環境なら、代わりに https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist からx64版をダウンロードして実行してもらう。

5. マーケットプレイスの追加とプラグインのインストール
   `claude plugin marketplace list` を実行し、`calm-marketplace` が無ければ `claude plugin marketplace add isizono/calm` を実行する。既にある場合は何もしなくてよい（更新したい場合のみ `claude plugin marketplace update calm-marketplace`）。
   次に `claude plugin install calm@calm-marketplace` を実行する（既にインストール済みなら `claude plugin update calm@calm-marketplace` で更新する）。成功メッセージが出ることを確認する。

6. プラグインディレクトリの特定と依存解決
   `(Get-Content -Raw "$env:USERPROFILE\.claude\plugins\installed_plugins.json" | ConvertFrom-Json).plugins.'calm@calm-marketplace'[0].installPath` でインストール先ディレクトリを特定する（失敗したら `Get-ChildItem "$env:USERPROFILE\.claude\plugins\cache\calm-marketplace\calm"` で見る）。
   特定したディレクトリに対して、`cd`ではなく `uv sync --frozen --directory "<特定したディレクトリ>"`（手順2で確定させたuvの呼び方を使う）を実行する。`cd`はツール呼び出しをまたいで残るとは限らないため使わない。1分以上かかることがあるので、途中で止まったように見えても完了まで待つ。エラー無く終わり、そのディレクトリに`.venv`ができていることを確認する。

7. MCP接続の確認
   `claude mcp list` を実行し、`plugin:calm:calm` の行が `Connected` になっていることを確認する。
   もし「uvが見つからない」系のエラーで失敗した場合、それは手順2と同じ理由（自分の環境変数が古いまま）でClaude Code自身がMCPサーバー起動時に新しいuvを見つけられていないだけの可能性が高く、今のセッション内では直しようがない。その場合は原因調査を打ち切り、そのまま手順8に進んでよい。
   それ以外の理由で失敗・タイムアウトした場合は、`%USERPROFILE%\.claude\.claude-code-memory\logs\server.log` と `logs\server.stderr.log`（既定のDBパスの場合。`CALM_DB_PATH`を変更していればそのディレクトリ配下）の内容を読んで、原因を私に報告してほしい。自己判断で直せる単純な設定ミスでなければ、そこで止まって私に相談する。

8. 最後に人間へ依頼すること
   ここまで確認できたら、私に次の2つを頼んでほしい。
   - Claude Codeを一度再起動すること（手順2・3でインストール・設定した内容は、Claude Code自身のプロセスを起動し直さないと自分からは使えないため）
   - 再起動後のセッションで `/mcp` を実行し、calmサーバーが connected になっていることを確認すること
   これはClaude Code自身のセッションの再起動が要るため、今のセッションの中では完結できない。
```
