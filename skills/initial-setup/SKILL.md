---
name: initial-setup
description: CALMのインストール直後に1回行う初期セットアップ。前提条件の確認、ペルソナのヒアリング、使い方の案内。setupスキルから振り分けられる。
user-invocable: false
---

# initial-setup

インストール直後に1回行う。ゴールは「CALMが動く状態で、ユーザーが最初の一歩を踏み出せる」こと。

## Step 1: 前提条件の確認

1つずつ確かめ、結果をまとめて報告する。満たしていないものがあれば直し方を添える。

- **uv**: `uv --version` が通ること。通らなければ https://docs.astral.sh/uv/ を案内する
- **MCPサーバーとembeddingサーバー**: `uv run --no-sync --directory "${CLAUDE_PLUGIN_ROOT}" python "${CLAUDE_PLUGIN_ROOT}/scripts/restart_server.py" --status` を実行し、`mcp_server.running` と `embedding_server.running` を見る
  - embeddingサーバーは初回のencodeで起動する（モデル約290MBのダウンロードで時間がかかる）。`running: false` だけなら故障と扱わず、初回の検索後に再確認するよう伝える
  - `mcp_server.running: false` なら、`/mcp` からの再接続、直らなければ [restart](../restart/SKILL.md) を案内する
- **Pythonのsqlite拡張**: サーバーは起動時にsqlite-vecの読み込みを検査し、失敗すると起動しない。`mcp_server.running: true` ならこの検査は通過している。起動しない場合の直し方は `docs/troubleshooting.md` にある（Homebrew Python 3.12を使う）

## Step 2: ペルソナのヒアリング

auto-memoryに既存のユーザーペルソナ（userタイプ）があれば読み、十分ならこのStepを飛ばす。

無い・薄い場合は、会話で聞く。「あなたの職種は？」のような項目埋め型の質問はしない。「普段どんなことにClaude Codeを使ってる？　CALMで何をしたい？」のようにオープンに聞き、返答から職種・専門性・CALMで解決したいことをこちらで構造化する。案を一言で見せて、違えば直してもらう。

確認できたペルソナはauto-memoryのユーザーペルソナ（userタイプ）として記録する。project-setupはこの記録を読んで経路を選ぶ。

## Step 3: 使い方の案内

ペルソナに合わせて、次の3つだけを短く案内する。詳細は [man](../man/SKILL.md) に任せる。

- 基本サイクル: 作業を始めたら `/calm:activity-start`、再開は `/calm:check-in`、終わる前に `/calm:sync-memory`
- 新しいプロジェクトを始めるときは `/calm:setup`
- 困ったら `/calm:man`

## 範囲外

プロジェクトの知識フレーム作成は [project-setup](../project-setup/SKILL.md)、環境変数の調整は [env-config](../env-config/SKILL.md) の担当。ユーザーが続けて望んだときだけ渡す。
