"""ow worker / channel ランタイム状態の派生ビュー (MV) を扱うサブパッケージ。

旧 queue-t<topic_id>.md ファイルベースのworker管理を廃止し、cc-memory本体DB上に
event-sourcing reducer + projector の2層モデルで集約する設計（T53 Phase 1）。

モジュール構成:
- channels.py        : ow_channels CRUD
- workers.py         : ow_workers CRUD
- applied_msgs.py    : ow_applied_msg_ids CRUD (reducer idempotency)
- reducer.py         : ow_apply_state（純粋reducer、relay history → ow_*テーブル書き込みのみ）
- projector.py       : ow_project_activities（ow_workers → activities 同期、副作用許可）
- dashboard.py       : ow_dashboard render + extract_activity_line ヘルパー
"""
