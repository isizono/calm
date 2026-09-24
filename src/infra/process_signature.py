"""プロセスの起動時刻(ps -o lstart=)を取得する軽量ユーティリティ。

pidの一致だけでは、対象プロセスが終了した後に別プロセスが同じpidを再利用した
場合に誤って「同一プロセスがまだ生存している」と判定してしまう。起動時刻を
併せて照合することでこれを防ぐ。

`src/services/restart_service.py`・`hooks/recorder_marker.py` の双方が必要と
するが、どちらも重い依存(embedding_service・git_repo等、あるいはStop hookの
毎回の起動コスト)を経由せずにこの1関数だけを使いたいため、ここに切り出す。
"""
from __future__ import annotations

import subprocess

SUBPROCESS_TIMEOUT_SEC = 5.0


def process_start_signature(
    pid: int, *, timeout_sec: float = SUBPROCESS_TIMEOUT_SEC
) -> str | None:
    """プロセスの起動時刻を返す。プロセスが存在しなければNone。

    ps呼び出しがタイムアウトした場合もNone(呼び出し元は「わからない」を
    「別プロセスである」側に倒す)。
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, check=False, timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return None
    output = result.stdout.strip()
    return output or None
