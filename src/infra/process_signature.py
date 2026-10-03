"""プロセスの起動時刻を取得する軽量ユーティリティ。

pidの一致だけでは、対象プロセスが終了した後に別プロセスが同じpidを再利用した
場合に誤って「同一プロセスがまだ生存している」と判定してしまう。起動時刻を
併せて照合することでこれを防ぐ。戻り値は2回の呼び出しを等価比較するための
不透明な文字列であり、特定のフォーマットとして解析されることを想定しない。

`src/services/restart_service.py`・`hooks/recorder_marker.py` の双方が必要と
するが、どちらも重い依存(embedding_service・git_repo等、あるいはStop hookの
毎回の起動コスト)を経由せずにこの1関数だけを使いたいため、ここに切り出す。
"""
from __future__ import annotations

import psutil


def process_start_signature(pid: int) -> str | None:
    """プロセスの起動時刻を返す。プロセスが存在しなければNone。"""
    try:
        return str(psutil.Process(pid).create_time())
    except psutil.Error:
        return None
