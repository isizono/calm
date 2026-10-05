# CALM

CALM（Concurrent, autonomous, loosely-coupled minds）は、複数のAIエージェントのセッションが、記録を介して互いの状況を知りながら協力して動けるようにすることを目指しているプラグインです。協力して動く部分はまだほとんど実現できておらず、現状で使えるのは、セッションをまたいで議論の文脈・決定事項・作業状況を引き継ぐ部分が中心です。

現状のメイン実装はClaude Codeで、Codex向けの対応は実装中です。

## 使うとどうなるか

CALMを使うとき、人間側がトピックや決定事項を直接操作することはありません。普段どおり話しかけるだけで、AIが記録し、必要なときに参照します。

- 新しいセッションを開いても、AIは進行中の作業を把握した状態で会話を始めます。前回どこまで進めたかを一から説明し直す必要がありません。
- 作業を始めるとき、AIが会話の内容から「終わったと言えるのはどんな状態か」を条件として書きます。条件が満たされると、AIはその作業を完了として扱います。
- 会話の途中で記録が途切れると、CALMがAIに記録を促します。AIはそれを受けて記録に戻ります。
- 「今どうなってる？」と聞くと、動いている作業・最近終わった作業・人間の判断を待っている項目・残っている作業をまとめて見せてくれます。
- AIがその場で判断できないことに出会うと、後で答えてほしい問いとして記録に残ります。答えると、尋ねたセッションが続きを進めます。

## 開発中のこと

一人のセッションで抱えきれない作業を複数のセッションに分けて進めることや、同時に動く別のセッションと記録を介して非同期にやり取りすることは、試験的に動き始めている段階です。

Codex向けの対応は[マイルストーン](https://github.com/isizono/calm/milestone/1)で進めています。

## クイックスタート

Claude Code向けの最小手順です。

### 前提条件

- [uv](https://docs.astral.sh/uv/) がインストールされていること
- Claude Code v2.1.139以上
- Python 3.12+（SQLite拡張ロード対応ビルドが必要）
  - pyenvのデフォルトビルドは非対応なため、Homebrew Python (`brew install python@3.12`) を推奨します

### インストール

```bash
claude plugin marketplace add isizono/calm
claude plugin install calm
```

### 動作確認

1. Claude Codeで`/mcp`を実行し、calmサーバーがconnected状態であることを確認します
2. `/calm:man`を実行し、使い方の案内が返ってくることを確認します（名前衝突がなければ`/man`でも呼び出せますが、確実に通るのは`/calm:man`です）

初回起動時はembeddingモデル（約290MB）をダウンロードするため時間がかかります。これはネットワーク接続が必要な一度きりの処理で、2回目以降はキャッシュされるため高速になります。

### Windows 11での利用

Windows 11（PowerShell）でもmacOS/Linuxと同じ手順でインストールできます。前提条件（Git for Windowsなど）、インストール手順、状態確認・停止、社内プロキシ環境での設定は[docs/windows-setup.md](docs/windows-setup.md)にまとめています。

## Codexで古いモデルを使う場合

CALMのMCPツール定義は59本・約12万バイトあり、全部がモデルへ渡されると会話のたびにコンテキストを大きく占めます。Codexはtool searchが使えるモデル（OpenAIのドキュメントではgpt-5.4以降）ではツールを必要になってから読み込みますが、それより前のモデルでは全ツール定義が毎回載る可能性があります。

その場合は、Codexの`~/.codex/config.toml`でCALMのMCPサーバーに`disabled_tools`（`enabled_tools`の後に適用される拒否リスト）を設定すると、普段使わないツールを隠せます。次の例は、他インスタンスとの記録の受け渡し、タグの整理、前提の揺らぎ管理、補助のツールを隠します。

次の例はCALMをCodexのプラグインとして入れている場合の書き方です。テーブル名の`plugins."calm@calm-marketplace".mcp_servers.calm`は`plugins."プラグイン名@マーケットプレイス名".mcp_servers.<サーバー名>`の形です。

```toml
[plugins."calm@calm-marketplace".mcp_servers.calm]
disabled_tools = [
  "collect_export_candidates", "export_bundle", "import_bundle", "set_instance_identity",
  "analyze_tags", "demote_tag_notes",
  "resolve_destabilization", "suggest_destabilized_candidates",
  "export_material",
]
```

MCPサーバーとして手動で登録している場合は、テーブル名を登録したサーバーのキー（`calm`という名前で登録したなら`[mcp_servers.calm]`）に差し替え、`disabled_tools`は同じ内容を書きます。既存のcommandやurlなどの設定はそのまま残します。

隠したツールを使うスキル（`/memory-export`、`/memory-import`、`/tag-cleanup`）は動かなくなります。`/audit`や`/remember`などでタグのnotesを縮める手順も、`demote_tag_notes`を隠すと実行できません。必要になったらそのツールを一覧から外してください。設定項目の詳細は[Codexの設定リファレンス](https://developers.openai.com/codex/config-reference)を参照してください。ツール名の一覧は[リファレンス](docs/reference.md)にあります。

## 仕組みの概要

CALMが記録する情報には次の種類があります。

- **トピック** — 議論の主題ごとに情報をまとめます
- **決定事項** — 合意した内容を理由とともに保存します
- **ログ** — 議論の経緯や検討過程を保存します
- **アクティビティ** — 作業タスクの進捗をステータスで追跡します。終わりの条件（goal）を紐づけることもできます
- **資材** — セッション中に生成された分析結果・ドラフト等を、タグ付きの独立したエンティティとして保存します
- **タグ** — トピック・決定事項・ログ・アクティビティを横断的に分類します。タグにnotesを付けると、作業開始時にAIへ自動で注入されます
- **振る舞い（habits）** — セッション共通の運用ルールを保存し、セッション開始時にAIへ配信します

これらはキーワード検索とベクトル検索を組み合わせたハイブリッド検索で横断的に参照できます。

## 詳しい資料

- [セットアップ](docs/setup.md) — インストールすると何が起きるか、環境変数による設定
- [リモートサーバー](docs/remote-server.md) — claude.ai（Web版）から接続するための構成手順
- [トラブルシューティング](docs/troubleshooting.md) — 動作確認の手順、よくある詰まりとその対処
- [リファレンス](docs/reference.md) — MCPツール一覧、スキル一覧

## ライセンス

MIT
