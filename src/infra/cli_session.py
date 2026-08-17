"""Claude Code CLI プロセスが公開する session file の読み取り。

このファイルのスキーマは Claude Code CLI 内部のものであり、cc-memory との
公開契約ではない。CLI のバージョンアップでフィールド名・形が変わりうるため、
全フィールドを型チェック付きの `.get()` で読み、想定外の形は None（=解決失敗）
へ倒す。本モジュールは読み取り専用アクセサであり、書き込みは一切行わない。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

from src.infra.lock_file import is_process_alive

CLAUDE_SESSIONS_DIR_ENV = "CCM_CLAUDE_SESSIONS_DIR"


def sessions_dir() -> Path:
    """Claude Code CLI の session file 置き場（既定 ``~/.claude/sessions``）。"""
    raw = os.environ.get(CLAUDE_SESSIONS_DIR_ENV)
    return Path(raw).expanduser() if raw else Path.home() / ".claude" / "sessions"


def read_cli_session(pid: int) -> Optional[dict]:
    """``<pid>.json`` を読み、CLI セッションの表示情報を返す。

    次のいずれかに該当したら None を返す:

    - ファイル不在 / JSON として壊れている / dict でない
    - 中身の ``pid`` がファイル名の pid と一致しない（取り違え防止）
    - ``name`` が非空文字列でない（別名解決の入力にならない）
    - pid のプロセスが生存していない（PID 再利用の一次防御）
    """
    path = sessions_dir() / f"{pid}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("pid") != pid:
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    if not is_process_alive(pid):
        return None
    cli_session_id = data.get("sessionId")
    cwd = data.get("cwd")
    cli_status = data.get("status")
    return {
        "cli_pid": pid,
        "name": name.strip(),
        "cli_session_id": cli_session_id if isinstance(cli_session_id, str) else None,
        "cwd": cwd if isinstance(cwd, str) else None,
        "cli_status": cli_status if isinstance(cli_status, str) else None,
    }


def find_cli_session(pids: Iterable[int]) -> Optional[dict]:
    """pid 列を先頭から順に見て、最初に解決できた CLI セッションを返す。

    呼び出し側は「自分に近い順」で pid を渡すこと。claude が claude を起動する
    入れ子ケースでは、最も内側（直近の親）の CLI が選ばれる。
    """
    for pid in pids:
        session = read_cli_session(pid)
        if session is not None:
            return session
    return None


__all__ = [
    "CLAUDE_SESSIONS_DIR_ENV",
    "sessions_dir",
    "read_cli_session",
    "find_cli_session",
]
