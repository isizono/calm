# Windows 11 セットアップ手順

対象はWindows 11（x64・ARM64）。Windows PowerShell 5.1とPowerShell 7のどちらでも動くように書く（`&&`を使わない等）。

前提として、Claude Code（ネイティブ版）がインストール済みでログイン済みであること。加えて[Git for Windows](https://gitforwindows.org/)が必要（マーケットプレイス`isizono/calm`はGitHubでホストされており、`claude plugin marketplace add`・`update`やプラグインのインストールのたびにClaude Codeが利用者側の`git`でcloneするため）。calmのhookはexec form（コマンドと引数を分けた形）で登録されておりシェルを経由しないため、hook自体の実行はGit for Windowsの有無に影響されない。Git for Windowsが入っていると、Claude Codeはスキルの中のシェル手順をBashツール（Git Bash経由）で実行できるようになる。これとは別に、PowerShellツールもclaude.ai・Consoleアカウントでは既定で有効になる。

社内プロキシ環境を使っている場合は、uvの導入（パターン1・パターン2とも手順3）より前に[README.mdの社内プロキシ環境での注意](../README.md#windows-11での利用)の設定（`HTTPS_PROXY`・`NO_PROXY`・`SSL_CERT_FILE`のユーザー環境変数への設定）を済ませておく。

2つの手順を用意する。

- [パターン1](#パターン1-人がpowershellで実行する手順): 人がPowerShellで1行ずつ実行する手順
- [パターン2](#パターン2-claude-codeに貼り付けるプロンプト): Claude Codeに貼り付けて、確認・実行・成功確認を自走させるプロンプト

状態確認・停止・lockの後始末は[README.mdのWindows 11の節](../README.md#windows-11での利用)に既にあるため、本ドキュメントでは重複させずリンクする。

## パターン1: 人がPowerShellで実行する手順

### 1. アーキテクチャの確認

以降の手順（特にARM64向けのx64 Python導入）で分岐するため、先に確かめる。

```powershell
$env:PROCESSOR_ARCHITECTURE
```

成功の目印: `AMD64`（x64機）または`ARM64`が返る。

### 2. Git for Windowsの導入

```powershell
winget install --id Git.Git -e --source winget
```

マシン全体への導入が既定のため、UACの確認ダイアログが出たら許可する。導入後は新しいPowerShellウィンドウを開き直す（同じウィンドウのままだと`git`コマンドがPATHに反映されない）。

確認:

```powershell
git --version
```

成功の目印: バージョン番号が表示される。

### 3. uvの導入とPATHの反映

まず試す。Claude Codeネイティブ版の導入時に`%USERPROFILE%\.local\bin`が既にPATHに入っていることが多く、uvを導入済みならこれだけで通る。

```powershell
uv --version
```

通らない場合は公式インストーラでインストールする。

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

インストール直後は現在のPowerShellウィンドウにPATHが反映されない。**新しいPowerShellウィンドウを開き直す**（最も確実）か、現在のウィンドウのままなら以下でPATHを読み直す。

```powershell
$env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')
```

再度確認する。

```powershell
uv --version
```

成功の目印: バージョン番号が表示される。

### 4. Visual C++再頒布可能パッケージ（x64）の導入

CALMが依存するtorchのimportに必要。無いと`c10.dll`のロードに失敗しWinError 126になる。ARM64機でも、x64エミュレーション下で動くtorchである以上この再頒布可能パッケージが要る。

```powershell
winget install -e --id Microsoft.VCRedist.2015+.x64 --accept-source-agreements --accept-package-agreements --silent
```

`--silent`はインストーラ自体のUIを抑止するだけで、管理者権限への昇格（UAC）は別に発生しうる。UACの確認ダイアログが出たら許可する。既に同じか新しいバージョンが入っている場合は`No available upgrade found`のようなメッセージで終わることがあるが、これも成功として扱ってよい。

winget自体が無い環境では、[Microsoft公式の配布ページ](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist)からx64版のインストーラを手動でダウンロードして実行する。

実際に効いているかの最終確認は、7.でtorchが読み込めることで行う。

### 5. ARM64の場合だけ: x64のCPythonとUV_PYTHON

ARM64版Windowsでは、CALMの依存（torch・sqlite-vec・cryptography）に`win_arm64`向けのwheelが無いため、uvが既定で選ぶARM64版のCPythonでは動かない。x64版のCPythonをuvで入れ、x64エミュレーション（Prism）上で動かす。

```powershell
uv python install cpython-3.12-windows-x86_64-none
```

次に、`uv sync`を実行するシェルだけでなく、Claude Codeが起動する`uv`からも見える必要があるため、ユーザー環境変数として永続化する。

```powershell
[System.Environment]::SetEnvironmentVariable('UV_PYTHON', 'cpython-3.12-windows-x86_64-none', 'User')
```

この設定はこのマシン上の全てのuvプロジェクトに効く。他のuvプロジェクトで別のPythonバージョンを使っている場合、そちらが動かなくなることがある。

設定後は新しいPowerShellウィンドウを開き直してから確認する。

```powershell
$env:UV_PYTHON
```

成功の目印: `cpython-3.12-windows-x86_64-none`が返る。

### 6. マーケットプレイスの追加とプラグインのインストール

既に追加済みかを確認する。

```powershell
claude plugin marketplace list
```

一覧に`calm-marketplace`が無ければ追加する。

```powershell
claude plugin marketplace add isizono/calm
```

既にある場合も、最新化のため常に以下を実行する（サードパーティのマーケットプレイスは自動更新が既定でオフのため、古いチェックアウトのままだと更新前の版がインストールされることがある）。

```powershell
claude plugin marketplace update calm-marketplace
```

プラグインをインストールする。

```powershell
claude plugin install calm@calm-marketplace
```

既にインストール済みの場合も、常に以下で更新する。

```powershell
claude plugin update calm@calm-marketplace
```

成功の目印: インストール（または更新）の成功メッセージが表示される。`claude plugin list`でも`calm@calm-marketplace`が確認できる。

### 7. プラグインのディレクトリを特定し`uv sync --frozen`、torchのimport確認

初回の依存取得（`uv sync`）は1分以上かかることがあり、Claude CodeのMCP接続待ち（30秒）を超えて`/mcp`の初回接続が失敗することがある。プラグインを入れた直後に、プラグインのディレクトリで`uv sync --frozen`を一度手動実行しておく。

ディレクトリは`%USERPROFILE%\.claude\plugins\cache\calm-marketplace\calm\<版>\`の形をしている。`<版>`の部分は`installed_plugins.json`から特定し、そのまま`uv sync --frozen`に渡す。

```powershell
$dir = (Get-Content -Raw -Encoding UTF8 "$env:USERPROFILE\.claude\plugins\installed_plugins.json" | ConvertFrom-Json).plugins.'calm@calm-marketplace'[0].installPath
uv sync --frozen --directory "$dir"
```

特定に失敗する場合は`Get-ChildItem`で直接見る。

```powershell
Get-ChildItem "$env:USERPROFILE\.claude\plugins\cache\calm-marketplace\calm"
```

成功の目印: エラー無く完了し、`$dir`のディレクトリに`.venv`が作成される。

続けて、torchが実際にimportできることを確認する。

```powershell
uv run --no-sync --directory "$dir" python -c "import torch; print(torch.__version__)"
```

成功の目印: バージョン番号が表示される。`WinError 126`や`c10.dll`に関するエラーが出た場合は、4.のVisual C++再頒布可能パッケージが正しく入っているか確認する。

### 8. `claude mcp list`で接続を確認

```powershell
claude mcp list
```

成功の目印: `plugin:calm:calm`の行が`Connected`と表示される（torchの読み込みは7.で確認済みなので、ここではMCP接続だけを見ればよい）。

### 9. Claude Codeを起動し直し、`/mcp`とフックの動作を確かめる

今使っているターミナルウィンドウ（Claude Codeのセッション）を閉じ、**新しいターミナルウィンドウで`claude`を起動し直す**（同じウィンドウで`/exit`してから`claude`を起動すると、このセッション内で設定した環境変数を引き継がないことがある。プラグインもセッション開始時にしか読み込まれないため、いずれにせよ再起動が要る）。

別のPowerShellウィンドウから以下を確認する（Claude Codeの中で`!`を付けて実行すると既定ではGit Bash経由になり、PowerShellのコマンドとしては動かないため）。

```powershell
Test-Path "$env:USERPROFILE\.claude\rules\cc-memory-habits.md"
```

新しいClaude Codeのセッション内では以下を確認する。

1. `/mcp`を実行し、calmサーバーがconnected状態であることを確認する
2. `/man`を実行し、使い方の案内が返ってくることを確認する（MCPツール呼び出しの疎通確認を兼ねる）
3. ARM64機では`$env:UV_PYTHON`が`cpython-3.12-windows-x86_64-none`を返すことを確認する

1.と2.はREADME.mdの[正常に動いているかの確認](../README.md#正常に動いているかの確認)と同じ内容である。

## うまくいかないとき

- **MCPの接続ログ**: `%LOCALAPPDATA%\claude-cli-nodejs\Cache\<cwd>\mcp-logs-plugin-calm-calm\`にClaude Code側のMCP接続ログが残る。
- **CALMのサーバーログ**: 既定のDBパス（`%USERPROFILE%\.claude\.claude-code-memory\`）と同じフォルダの`logs\server.log`（Pythonの`logging`経由の通常ログ。起動確認・DB初期化等）と`logs\server.stderr.log`（起動直後に落ちるような致命的失敗用。起動のたびに上書きされるため、再試行する前に読む。launcherはサーバープロセスの標準出力をDEVNULLに捨てるため、標準出力自体にはログは出ない）に書かれる。`CALM_DB_PATH`を変更している場合はそのディレクトリ配下になる。
- **embeddingサーバーのログ**: `%USERPROFILE%\.cache\cc-memory\embedding-server.log`。torchのimportに失敗している場合、このログに`c10.dll`関連のエラーが出ていないか確認する。
- **残ったlockファイルの扱い**: README.mdの[状態確認・停止・lockの後始末](../README.md#windows-11での利用)を参照。
- **社内プロキシ環境での注意**: README.mdの[社内プロキシ環境での注意](../README.md#windows-11での利用)を参照。
- **初回の`uv sync`を忘れたときの症状**: 初回の`/mcp`接続がタイムアウトする。7.の`uv sync --frozen`を手動実行してから`/mcp`を再接続する。

## パターン2: Claude Codeに貼り付けるプロンプト

前提はパターン1と同じ（Claude Code ネイティブ版、ログイン済み）。PowerShellでClaude Codeを起動し、以下のプロンプトを丸ごと貼り付ける。プロンプトは秘密情報（トークン等）を扱わない。`irm | iex`の実行や環境変数の永続化など、実行の許可を求められることがあるので、内容を確認した上で許可する。

```
CALM（isizono/calm）をこのWindows機にセットアップしてほしい。各手順は「確認 → 必要なら実行 → 成功の確認」の順で進め、失敗したら次の手順には進まず、何が起きたかと必要な対処を私に伝えて止まってほしい。秘密情報（トークン等）の入力や送信は不要なので扱わないこと。社内プロキシが必要そうな兆候（uvやgitのダウンロードが止まる、証明書エラーが出る等）が見えても、プロキシのURLや証明書を推測で設定せず、必ず私に確認すること。`claude`のサブコマンド（`claude plugin`・`claude mcp`等）の実行が自分のツールから拒否される場合は、その場で諦めず、私に別のPowerShellウィンドウで同じコマンドを実行してもらうよう頼む。

0. 実行環境の確認
   まず自分が使えるツールがPowerShellツールかBashツール（Git Bash経由）かを確認する。PowerShellツールがあるなら、必ずそちらを使う。
   Bashツールしか無い場合、PowerShellコマンドを `powershell -Command "..."` のように二重引用符でそのまま埋め込まない。Bash自身が`$env:...`のような`$`始まりの部分を実行前に展開してしまい、コマンドが壊れる。代わりに、実行したいPowerShellコードを一度ファイルに書き出してから `-File` で渡す。ファイルに書き出すときは、ヒアドキュメントの終端記号をクォートしてBashの変数展開を止める（例: `cat <<'PS1' > /tmp/step.ps1` のように終端を `'PS1'` とクォートする）。実行は `powershell -NoProfile -ExecutionPolicy Bypass -File /tmp/step.ps1` のように行う。このときps1の中身には日本語などASCII以外の文字を書かない（Git Bashが書き出すファイルはBOM無しUTF-8になり、PowerShell 5.1がそれをCP932として読んで構文エラーになることがある）。

   以降の手順で新しくPowerShellのコマンド・スクリプトを実行するときは、呼び出しのたびに先頭で次を実行してからそのコマンドを呼ぶこと。自分（Claude）のツール呼び出しは毎回新しいプロセスで動き、直前の別のツール呼び出しでの環境変数の変更は自動では反映されないため（Claude Codeのツールリファレンスに明記されている仕様）。

   $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')

   手順4でARM64向けのUV_PYTHONを設定した後は、これに加えて `$env:UV_PYTHON = 'cpython-3.12-windows-x86_64-none'` も毎回セットしてから呼ぶ。

1. アーキテクチャの確認
   `$env:PROCESSOR_ARCHITECTURE` を確認し、x64（AMD64）かARM64かを把握する。以降の手順4はARM64のときだけ実行する。

2. Gitの確認
   `git --version` を試す。失敗する場合、次のコマンドを提示して人間に実行してもらうよう頼む（マシン全体への導入が既定でUACの確認が必要なため）。

   winget install --id Git.Git -e --source winget

   人間の実行が終わったら報告を待ち、0.のPATH再読み込みを行ってから`git --version`を確認する。

3. uvの導入
   `uv --version` を試す。失敗する場合のみ、公式インストーラ `irm https://astral.sh/uv/install.ps1 | iex` を実行し、0.のPATH再読み込みを行ってから`uv --version`で確認する。

4. （ARM64のときだけ）x64のCPythonとUV_PYTHON
   `uv python install cpython-3.12-windows-x86_64-none` を実行する（手順0のPATH再読み込みを使う）。続けて `[Environment]::SetEnvironmentVariable('UV_PYTHON', 'cpython-3.12-windows-x86_64-none', 'User')` でユーザー環境変数として永続化する。この設定はこのマシン上の全てのuvプロジェクトに効くため、既に`UV_PYTHON`が別の値に設定されている場合は、上書きする前に私に確認すること。

5. マーケットプレイスの追加とプラグインのインストール
   `claude plugin marketplace list` を実行し、`calm-marketplace` が無ければ `claude plugin marketplace add isizono/calm` を実行する。既にある場合も常に `claude plugin marketplace update calm-marketplace` を実行する（サードパーティのマーケットプレイスは自動更新が既定でオフのため）。
   次に `claude plugin install calm@calm-marketplace` を実行する。既にインストール済みの場合も常に `claude plugin update calm@calm-marketplace` を実行する。成功メッセージが出ることを確認する。

6. プラグインディレクトリの特定と依存解決
   `(Get-Content -Raw -Encoding UTF8 "$env:USERPROFILE\.claude\plugins\installed_plugins.json" | ConvertFrom-Json).plugins.'calm@calm-marketplace'[0].installPath` でインストール先ディレクトリを特定する（失敗したら `Get-ChildItem "$env:USERPROFILE\.claude\plugins\cache\calm-marketplace\calm"` で見る）。
   特定したディレクトリに対して、`cd`ではなく `uv sync --frozen --directory "<特定したディレクトリ>"`（手順0で読み直したPATH・UV_PYTHONを使う）を実行する。1分以上かかることがあるので、timeoutを600000ms（10分）に指定して実行する。timeoutで打ち切られた場合は、同じコマンドをもう一度実行する（`--frozen`なので再実行しても安全）。エラー無く終わり、そのディレクトリに`.venv`ができていることを確認する。

7. torchのimport確認
   `uv run --no-sync --directory "<上で特定したディレクトリ>" python -c "import torch; print(torch.__version__)"` を実行する。バージョン番号が表示されれば成功。
   `WinError 126`や`c10.dll`に関するエラーが出た場合、Visual C++再頒布可能パッケージ（x64）が無い可能性が高い。次のコマンドを提示して人間に実行してもらうよう頼む（管理者権限とUACの確認が必要なため）。

   winget install -e --id Microsoft.VCRedist.2015+.x64 --accept-source-agreements --accept-package-agreements --silent

   wingetが無い環境なら、代わりに https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist からx64版をダウンロードして実行してもらう。人間の実行が終わったら報告を待ち、torchのimportをやり直す。

8. MCP接続の確認
   手順0のPATH・UV_PYTHON再読み込みを行ってから `claude mcp list` を実行し、`plugin:calm:calm` の行が `Connected` になっていることを確認する。
   それでも失敗・タイムアウトした場合は、`%USERPROFILE%\.claude\.claude-code-memory\logs\server.log` と `logs\server.stderr.log`（既定のDBパスの場合。`CALM_DB_PATH`を変更していればそのディレクトリ配下）の内容を読んで、原因を私に報告してほしい。調査はログを読むだけに留め、次のいずれも行わないこと: `.claude-code-memory`配下のファイルの削除・移動、プラグインキャッシュ内のファイルの編集、プロセスの停止、lockファイルの削除、マシンスコープの環境変数の変更、管理者権限での実行。これらが必要そうに見えても、そこで止まって私に相談する。

9. 最後に人間へ依頼すること
   ここまで確認できたら、私に次の2つを頼んでほしい。
   - 今のターミナルウィンドウを閉じ、新しいウィンドウでClaude Codeを起動し直すこと（同じウィンドウで`/exit`してから`claude`を起動すると、このセッションで設定した環境変数を引き継がないことがある。プラグインもセッション開始時にしか読み込まれないため、いずれにせよ再起動が要る）
   - 再起動後のセッションで `/mcp` を実行し、calmサーバーが connected になっていることを確認すること（ARM64機では合わせて `$env:UV_PYTHON` も確認する）
   これはClaude Code自身のセッションの再起動が要るため、今のセッションの中では完結できない。
```
