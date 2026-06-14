"""relay（中継サーバー）パッケージ。

cc-memoryリポ内にvendoringされたrelayサーバー。元は isizono/relay リポにあったが、
版ずれ・配布不整合によるowフレームワークの障害（2026-06-14: relayが古いコードで
全worker 404を返した）の根本対策として、リポ内固定パスにハードフォークした。

`PROTOCOL_VERSION` はメッセージスキーマ・エンドポイント契約のバージョン。
ow_service側からも同じ定数を import するため、版ずれが構造的に発生しない。
互換性が壊れる変更時のみ手動でインクリメントする。
"""

PROTOCOL_VERSION = 1
