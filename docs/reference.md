# リファレンス

[← README](../README.md)

MCPツールの詳細仕様は[docs/spec/mcp-tools.md](spec/mcp-tools.md)にあります。

## MCPツール

| カテゴリ | ツール | 説明 |
|---------|--------|------|
| トピック | `add_topic`, `get_topics` | 議論トピックの作成・新しい順の取得 |
| 議論ログ | `add_logs`, `get_logs` | 議論の経緯や検討過程の一括記録・取得 |
| 決定事項 | `add_decisions`, `get_decisions`, `pull_precedents` | 合意内容の記録・取得、設計判断前の近傍トピック判例の網羅確認 |
| アクティビティ | `add_activity`, `get_activities`, `update_activity` | 作業タスクの作成・取得・状態更新 |
| check-in | `check_in` | アクティビティにcheck-inし、tag notes・資材・関連decisionsを集約取得 |
| 資材 | `add_material`, `update_material`, `get_material`, `export_material` | セッション中の成果物をタグ付き独立エンティティとして保存・更新・取得・md出力 |
| リレーション | `add_relation`, `remove_relation`, `get_map` | エンティティ間の関連の追加・削除・グラフ探索 |
| 前提の揺らぎ管理 | `resolve_destabilization`, `suggest_destabilized_candidates` | 軸変更によりdestabilizeされたdecisionの解消・候補提示 |
| 振る舞い | `add_habit`, `get_habits`, `update_habit` | check-in時に注入される運用ルールの登録・取得・更新 |
| 終了条件（goal） | `set_goal`, `update_goal`, `judge_goal`, `get_goal` | アクティビティの終了条件の設定・条件の追加や状態変更・達成/失敗の判定・全条件と紐づくアクティビティの取得 |
| タグ | `search_tags`, `update_tag`, `analyze_tags`, `demote_tag_notes` | タグの検索、notes・エイリアス・退役状態等の更新、タグ共起分析、notesの指定セクションの資材への退避 |
| ピン | `add_pin`, `remove_pin` | エンティティ間のpin（強調的な関連付け）の追加・削除 |
| 取り消し | `retract` | 決定事項・ログ・資材の論理削除 |
| 検索・横断参照 | `search`, `get_by_ids`, `get_timeline`, `get_overview` | キーワード横断検索、詳細情報の一括取得、時系列表示、進行中/直近完了/裁定待ち/残件の内訳を一望取得 |
| セッション | `get_sessions`, `set_session_alias` | 稼働中セッションの表示名→別名の対応表取得、自セッションの別名の付け替え |
| シグナル・計測 | `report_signal`, `get_signals`, `update_signal`, `detect_reask_candidates` | calm自身への故障報告・矛盾検出・聞き返し候補検出等の運用計測 |
| Ask（人間への判断委譲） | `add_ask`, `get_asks`, `answer_ask`, `triage_ask`, `withdraw_ask`, `unsubscribe_ask` | 離席中・セッション跨ぎの判断待ち問いの起票・取得・回答・振り分け・取り下げ・通知解除 |
| フィードバック | `get_feedback_entries`, `write_feedback_entry`, `add_feedback_note` | 発話・ツール失敗・実行直前に配達するフィードバックエントリの取得・作成/変更/削除・ノート追加 |
| その他 | `get_config`, `roll_dice` | 設定値の取得、ダイスロール |

## スキル

| スキル | 説明 |
|--------|------|
| `/man` | CALMの使い方をAIが説明します |
| `/overview` | 進行中・直近完了・裁定待ち・残件の内訳を一望表示します |
| `/project-setup` | 新しいプロジェクト・取り組みの知識フレームをCALMにセットアップします |
| `/coding-project-setup` | コードプロジェクト向けの知識フレームをセットアップします（project-setupから委譲） |
| `/activity-start` | 新しいアクティビティを開始します |
| `/activity-pause` | 進行中のアクティビティを完了にせず中断します |
| `/activity-finish` | アクティビティを完了にします |
| `/check-in` | アクティビティにcheck-inして関連情報を集約取得します |
| `/decision-record` | ユーザーとの合意事項をdecisionとして記録するようガイドします |
| `/recording` | 議論の経緯や成果物をログ・資材として記録するようガイドします |
| `/remember` | 「覚えて」等の依頼を受けて、情報の保存先を判定します |
| `/forget` | 現状と矛盾・陳腐化した過去の記録を撤回します |
| `/rule-placement` | 一般化ルールの配置先（habit・tag notes・CLAUDE.md等）をfull評価で判定します |
| `/tag-notes` | タグのnotesを確認・更新します |
| `/tag-cleanup` | タグの共起分析を実行し、整理提案をユーザーに提示します |
| `/sync-memory` | セッション終了前にtranscriptを解析し、トピック・決定事項・ログ・アクティビティを一括で記録・更新します |
| `/digest` | 直近の記録を期間横断で俯瞰するダイジェストを生成します |
| `/postmortem` | completedアクティビティを振り返り、教訓を永続化します |
| `/audit` | 過去の決定事項の矛盾・陳腐化を検証し、知識を正しい場所に記録し直します |
| `/recompose-context` | アクティビティ・トピック等の関連情報を統合整理し、anchor対応表を作ります。整理範囲のアクティビティのgoal・親への結びつけも整えます。`--all` でアクティビティ(active/shelved/snoozed)全域を棚卸しし、completed化・shelved化・統合などの処遇に反映します |
| `/setup-anchor` | 合意事項の検証先（anchor）を対話的に確定・更新します |
| `/scribe` | CALMの記録からドキュメントを生成します |
| `/db-recovery` | DBデータの異常減少を検知した際に、スナップショットから復旧します |
| `/restart` | CALMのローカルMCPサーバー・embeddingサーバーを再起動します |
| `/ask-compose` | `add_ask`のquestion/contextをテンプレートに沿って構成するようガイドします |
| `/ask-distill` | 繰り返し起票されている同型のaskをまとめてメタaskを起票します |
| `/ask-answer` | open askを一覧して1件ずつ提示し、回答をanswer_askで記録します |
| `/ask-watch` | askストアを継続的に監視し、同型のaskが溜まっていたらメタaskとして起票します |
| `/board` | Claude同士の非同期のやり取り（質問・周知・意見募集・事前の声かけ）を掲示板トピックに投稿する使い方をガイドします |
| `/peer-nudge` | セッション台帳の宛先候補へSendMessageで直接話しかけるときの作法をガイドします |
| `/orch` | 仕事を複数の子に分けてbgに振り、まとめ役として追う・引き継ぐ・終わらせる手順をガイドします |
