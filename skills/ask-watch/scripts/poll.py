"""asksテーブルのopen件数・最新last_seen_at・id集合のいずれかが変化した瞬間だけ
1行出力するポーリングループ。

Windowsはsqlite3 CLIを同梱しないため、標準ライブラリのsqlite3モジュールを使う。
GROUP_CONCAT(id)まで比較に含めているのは、件数が同じでもid構成が入れ替わる変化
（1件closeして1件openになった等）を取りこぼさないため。
"""
from __future__ import annotations

import contextlib
import sqlite3
import sys
import time
from pathlib import Path

# プロジェクトルートをパスに追加（src.db参照用）
_project_root = Path(__file__).resolve().parents[3]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.db import get_db_path  # noqa: E402

POLL_INTERVAL_SEC = 10
_QUERY = "SELECT COUNT(*), MAX(last_seen_at), GROUP_CONCAT(id) FROM asks WHERE status='open'"


def _snapshot(db_path: str) -> tuple | None:
    """open askの件数・最新last_seen_at・id集合のスナップショットを1回取得する。

    DBロック・未作成・破損等でクエリに失敗した場合はNoneを返し、呼び出し側で
    このtickをスキップする(prevを巻き戻さず、次の正常tickとの比較で変化を
    取りこぼさないようにする)。sqlite3.connect()はファイルが無いと新規作成して
    しまうため、先に存在確認する(復元中でDBファイルが一時的に無い間に
    空ファイルを作ってしまわないため)。
    """
    if not Path(db_path).exists():
        return None
    try:
        with contextlib.closing(sqlite3.connect(db_path, timeout=5)) as conn:
            return conn.execute(_QUERY).fetchone()
    except sqlite3.Error as e:
        print(f"ask-watch poll: query failed, skipping tick: {e}", file=sys.stderr, flush=True)
        return None


def main() -> None:
    db_path = get_db_path()
    prev = _snapshot(db_path)
    while True:
        time.sleep(POLL_INTERVAL_SEC)
        cur = _snapshot(db_path)
        if cur is not None and cur != prev:
            print(f"ask store changed: {cur}", flush=True)
            prev = cur


if __name__ == "__main__":
    main()
