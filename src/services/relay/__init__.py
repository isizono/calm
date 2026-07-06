"""relay セッション面（セッション間通信の MCP tool 実装）パッケージ。

- config: 接続設定・状態ディレクトリの解決
- declarations: subscription declaration file の read/write
- inbox: per-session inbox（JSONL）の append/drain/cursor 管理
- service: 4 動詞（post / publish / subscribe / receive）の実装本体
"""
