"""relay 呼び出し元の安定 identity 解決。

cc-memory の caller_session_id は本来 MCP 接続単位の ephemeral な値
（fastmcp の `ctx.session_id`）であり、cc-memory server の再起動のたびに
新しい値へ切り替わる。

relay の declaration/inbox/subscription は「後から同じ相手に配達を続ける」
ことを前提にした永続状態であり、この ephemeral な値をキーにすると server
再起動のたびに宛先を見失う。

launcher.py（src/launcher.py）は Claude Code セッション（正確には launcher
プロセス）ごとに 1 度だけ発行する UUID を既に保持しており、この値を
X-CC-Memory-Bridge-Session-Id ヘッダとして全 MCP リクエストに同梱する。
本モジュールはこのヘッダを優先的に読み、relay 呼び出し元の識別子を解決する。

SessionStart hook（hooks/session_start_hook.py）は Claude Code CLI が直接
起動する独立プロセスであり、MCP リクエストコンテキスト自体を持たないため
上記ヘッダ方式では identity を解決できない。一方で launcher プロセスと hook
プロセスは、実行時の起動元をたどると同じ Claude Code CLI プロセスの子孫に
なる（uv ラッパー・シェル等の中間プロセスを挟むことはあるが、数段さかのぼれば
同一の CLI プロセスに合流する）。本モジュールはこの性質を使い、launcher が
自身の祖先 pid チェーンを登録しておいたファイルと、hook 側で計算した自分の
祖先 pid チェーンとの共通部分の有無で「同じセッションの launcher」を探す
（`resolve_identity_by_ancestry`）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from src.infra.lock_file import is_process_alive
from src.services.relay import config
from src.services.relay.declarations import now_iso

logger = logging.getLogger(__name__)

BRIDGE_SESSION_HEADER = "x-cc-memory-bridge-session-id"

# 祖先 pid チェーンをさかのぼる最大段数。launcher 側の登録ファイル生成・hook
# 側の解決の両方で共通して使う。
_MAX_ANCESTOR_DEPTH = 5

_REGISTRATION_PREFIX = "launcher-"
_REGISTRATION_SUFFIX = ".json"


def _ephemeral_session_id() -> Optional[str]:
    """MCP context から呼び出しセッションの session_id を取得する（ephemeral）。

    MCP のツール実行コンテキスト外（テスト等）では None を返す。
    """
    try:
        from fastmcp.server.dependencies import get_context

        return get_context().session_id
    except RuntimeError:
        return None


def get_relay_identity() -> Optional[str]:
    """relay 呼び出し元の識別子を解決する。

    launcher.py 経由（X-CC-Memory-Bridge-Session-Id ヘッダ）の呼び出しは、
    cc-memory server の再起動をまたいで不変な識別子を返す。ヘッダが無い
    呼び出し元（本ヘッダを付与しない MCP クライアント）、および HTTP
    リクエストコンテキスト外からの呼び出し（import失敗・get_http_headers()
    自体の失敗を含む）は、従来通り ctx.session_id（ephemeral、MCP 接続単位）
    にフォールバックする。
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers()
    except Exception:
        return _ephemeral_session_id()

    stable_id = headers.get(BRIDGE_SESSION_HEADER)
    if isinstance(stable_id, str) and stable_id.strip():
        return stable_id.strip()
    return _ephemeral_session_id()


def _get_ppid(pid: int) -> Optional[int]:
    """`ps` 経由で指定 pid の親 pid を取得する。

    標準ライブラリには移植可能な ppid 取得手段が無いため `ps -o ppid= -p <pid>`
    に頼る（macOS / Linux いずれの `ps` 実装でも動く POSIX オプション）。
    プロセス消滅・`ps` 不在・タイムアウト等はすべて None（呼び出し側は祖先探索を
    打ち切るだけで例外にはしない）。
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    try:
        return int(raw)
    except ValueError:
        return None


def ancestor_pids(pid: int, max_depth: int = _MAX_ANCESTOR_DEPTH) -> list[int]:
    """指定 pid から直近の親を先頭に、最大 max_depth 段の祖先 pid リストを返す。

    pid 1（launchd/init）はほぼ全プロセスの祖先チェーンに現れる普遍的な値で、
    これを含めると無関係なプロセス同士が「共通の祖先を持つ」と誤判定される
    ため、意図的にリストへ含めない（1 に到達した時点で探索を打ち切る）。
    """
    result: list[int] = []
    current = pid
    for _ in range(max_depth):
        ppid = _get_ppid(current)
        if ppid is None or ppid <= 1:
            break
        result.append(ppid)
        current = ppid
    return result


def _registration_path(pid: int) -> Path:
    return config.sessions_dir() / f"{_REGISTRATION_PREFIX}{pid}{_REGISTRATION_SUFFIX}"


def _gc_stale_launcher_registrations() -> None:
    """sessions dir 配下の登録ファイルのうち、pid が生存していないものを削除する。

    起動のたびに launcher が呼ぶ想定。壊れた JSON・想定外の型のファイルも
    ここで一掃する（読み側 resolve_identity_by_ancestry を単純に保つため）。
    """
    sessions_dir = config.sessions_dir()
    if not sessions_dir.exists():
        return
    for path in sessions_dir.glob(f"{_REGISTRATION_PREFIX}*{_REGISTRATION_SUFFIX}"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            continue
        pid = data.get("pid") if isinstance(data, dict) else None
        if not isinstance(pid, int) or not is_process_alive(pid):
            path.unlink(missing_ok=True)


def register_launcher_session(session_id: str, pid: Optional[int] = None) -> Optional[Path]:
    """launcher 起動時に自身の登録ファイルを書く。

    書込前に stale な登録ファイル（プロセスが生存していないもの）を GC する。
    書込に失敗しても例外は投げず None を返す（ファイル書込は launcher の
    stdio ブリッジ本体には影響させない、ベストエフォートの付帯機能のため）。
    """
    pid = pid if pid is not None else os.getpid()
    try:
        _gc_stale_launcher_registrations()
        sessions_dir = config.sessions_dir()
        sessions_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "pid": pid,
            "ancestor_pids": ancestor_pids(pid),
            "created_at": now_iso(),
        }
        # mkstemp は既定で 0600 を作るため、同一 dir 上の atomic rename で
        # その権限のまま最終パスへ収める。
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(sessions_dir), prefix=".launcher-", suffix=".json.tmp"
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, ensure_ascii=False)
            path = _registration_path(pid)
            os.replace(tmp_path, path)
            return path
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
    except OSError as e:
        logger.warning(f"Failed to write launcher session registration: {e}")
        return None


def unregister_launcher_session(pid: Optional[int] = None) -> None:
    """launcher 終了時に自身の登録ファイルを削除する（存在しなくても無害）。"""
    pid = pid if pid is not None else os.getpid()
    try:
        _registration_path(pid).unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"Failed to remove launcher session registration: {e}")


def resolve_identity_by_ancestry(
    pid: Optional[int] = None, max_depth: int = _MAX_ANCESTOR_DEPTH
) -> Optional[str]:
    """自プロセスの祖先 pid チェーンから、同じ Claude Code CLI プロセスを
    親に持つ launcher の identity（session_id）を解決する。

    MCP リクエストコンテキストを持たない呼び出し元（SessionStart hook 等）
    専用のフォールバック経路。get_relay_identity() のヘッダ/ctx.session_id
    経路が使えるコンテキスト（実際の MCP ツール呼び出し）では、そちらが
    launcher とは別プロセス（cc-memory HTTP server）で動くため祖先チェーンに
    意味がなく、本関数を使ってはならない。

    解決できない場合（登録ファイル不在・共通祖先なし・生存 launcher なし等）
    は None を返す（安全側フォールバック、例外は投げない）。
    複数の登録ファイルが共通祖先を持つ場合は created_at が新しい方を採用する。
    """
    pid = pid if pid is not None else os.getpid()
    own_ancestors = set(ancestor_pids(pid, max_depth))
    if not own_ancestors:
        return None

    sessions_dir = config.sessions_dir()
    if not sessions_dir.exists():
        return None

    candidates: list[tuple[str, str]] = []
    for path in sessions_dir.glob(f"{_REGISTRATION_PREFIX}*{_REGISTRATION_SUFFIX}"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        session_id = data.get("session_id")
        launcher_pid = data.get("pid")
        launcher_ancestors = data.get("ancestor_pids")
        if not isinstance(session_id, str) or not session_id:
            continue
        if not isinstance(launcher_ancestors, list):
            continue
        if not isinstance(launcher_pid, int) or not is_process_alive(launcher_pid):
            continue
        if own_ancestors & set(launcher_ancestors):
            candidates.append((str(data.get("created_at", "")), session_id))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


__all__ = [
    "BRIDGE_SESSION_HEADER",
    "get_relay_identity",
    "ancestor_pids",
    "register_launcher_session",
    "unregister_launcher_session",
    "resolve_identity_by_ancestry",
]
