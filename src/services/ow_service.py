"""ow（orch/worker）基盤サービス

relay HTTPサーバーとのやり取り、worker spawn/close、ステータス管理を担う。
外部HTTPのためcc-memory DBのconn共有パターンは不要。urllib.requestベース（サードパーティ依存なし）。
"""
import json
import logging
import os
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ----------------------------
# 設定定数
# ----------------------------

RELAY_URL = os.environ.get("RELAY_URL", "http://127.0.0.1:8765")
RELAY_DIR = os.environ.get("RELAY_DIR", str(Path.home() / "workspace" / "powwow"))
OW_QUEUE_DIR = os.environ.get("OW_QUEUE_DIR", "")

_MAX_RETRIES = 3


# ----------------------------
# relay HTTPヘルパー
# ----------------------------


def _relay_request(method: str, path: str, data: dict | None = None) -> dict:
    """relay HTTPリクエストの共通ヘルパー。4xx即失敗、5xx/接続断のみリトライ。

    Args:
        method: "GET" or "POST"
        path: relayエンドポイントパス（例: "/send", "/history?channel=...&since=0"）
        data: POSTボディ（Noneの場合はGET）

    Returns:
        レスポンスJSON dictまたは {"error": ...}
    """
    url = f"{RELAY_URL}{path}"
    body_bytes = json.dumps(data).encode("utf-8") if data is not None else None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body_bytes,
                headers={"Content-Type": "application/json"} if body_bytes else {},
                method=method,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code < 500:
                # 4xx: クライアントエラー → 即失敗（リトライしない）
                try:
                    err_body = json.loads(e.read())
                except Exception:
                    err_body = {"message": str(e)}
                return {"error": {"code": e.code, "message": err_body}}
            if attempt >= _MAX_RETRIES:
                raise
            sleep_secs = 2 ** attempt
            logger.warning(
                "relay %s %s returned %d, retrying in %ds (%d/%d)",
                method, path, e.code, sleep_secs, attempt + 1, _MAX_RETRIES,
            )
            time.sleep(sleep_secs)
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            if attempt >= _MAX_RETRIES:
                raise
            sleep_secs = 2 ** attempt
            logger.warning(
                "relay %s %s failed: %s, retrying in %ds (%d/%d)",
                method, path, e, sleep_secs, attempt + 1, _MAX_RETRIES,
            )
            time.sleep(sleep_secs)

    # ここには到達しない（ループ内でraiseされる）
    raise RuntimeError("unreachable")


# ----------------------------
# relayサーバー起動確認
# ----------------------------


def _is_relay_running() -> bool:
    """relayサーバーの生存確認。"""
    try:
        req = urllib.request.Request(
            f"{RELAY_URL}/presence?channel=__health__",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        # 404でもサーバー起動済みと判定可能
        return e.code in (404, 400)
    except Exception:
        return False


def _start_relay_server() -> bool:
    """relayサーバーをバックグラウンドで起動する。"""
    relay_dir = Path(RELAY_DIR).expanduser()
    server_py = relay_dir / "server.py"
    if not server_py.exists():
        logger.warning("relay server.py not found at %s", server_py)
        return False
    try:
        subprocess.Popen(
            [sys.executable, str(server_py)],
            cwd=str(relay_dir),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("relay server process started")
        return True
    except OSError as e:
        logger.warning("Failed to start relay server: %s", e)
        return False


def ensure_relay_server() -> bool:
    """relayサーバーの起動確認 → 未起動なら自動起動 → 待機。

    Returns:
        Trueなら接続可能、Falseなら起動失敗またはタイムアウト
    """
    if _is_relay_running():
        return True
    if not _start_relay_server():
        return False
    # 最大10秒待機（0.5秒間隔 x 20回）
    for _ in range(20):
        time.sleep(0.5)
        if _is_relay_running():
            logger.info("relay server is ready")
            return True
    logger.warning("relay server failed to start within 10 seconds")
    return False


# ----------------------------
# T1: ow_send
# ----------------------------


def ow_send(
    channel: str,
    handle: str,
    body: dict,
    needs_reply: bool = False,
    in_reply_to: int | None = None,
) -> dict:
    """ow channelにメッセージを送信する。

    bodyはow固有JSONを格納するdict（relay schemaは無改修）。
    4xx即失敗、5xx/接続断のみ3回指数バックオフ。

    Args:
        channel: channelコード
        handle: 送信者handle（例: "orch", "w-a"）
        body: ow固有JSON（{"v":1, "kind":"cmd"|"state", ...}）
        needs_reply: 返信を期待するか
        in_reply_to: 返信先のmsg_id

    Returns:
        成功時: {"msg_id": int}
        失敗時: {"error": {...}}
    """
    payload: dict = {
        "channel": channel,
        "handle": handle,
        "body": json.dumps(body),
        "needs_reply": needs_reply,
    }
    if in_reply_to is not None:
        payload["in_reply_to"] = in_reply_to

    return _relay_request("POST", "/send", payload)


# ----------------------------
# T2: ow_history
# ----------------------------


def ow_history(channel: str, since: int = 0, limit: int = 100) -> dict:
    """GET /history。受信処理の本体。

    since自身を含まない（msg_id > since）。

    Args:
        channel: channelコード
        since: このmsg_idより大きいものを返す（0=全件）
        limit: 最大取得件数

    Returns:
        {"messages": [{"msg_id": int, "handle": str, "body": dict, ...}, ...]}
        失敗時: {"error": {...}}
    """
    params = urllib.parse.urlencode({"channel": channel, "since": since, "limit": limit})
    path = f"/history?{params}"
    result = _relay_request("GET", path)
    if "error" in result:
        return result

    # bodyがJSON文字列の場合はパースする
    messages = result.get("messages", [])
    for msg in messages:
        if isinstance(msg.get("body"), str):
            try:
                msg["body"] = json.loads(msg["body"])
            except (json.JSONDecodeError, TypeError):
                pass  # パース失敗はそのまま文字列で返す

    return {"messages": messages}


# ----------------------------
# T3: ow_spawn_worker / ow_close_worker
# ----------------------------


def _get_queue_dir() -> Path | None:
    """queueディレクトリパスを返す（OW_QUEUE_DIR環境変数）。"""
    if OW_QUEUE_DIR:
        return Path(OW_QUEUE_DIR).expanduser()
    return None


def _write_queue_spawning(
    queue_dir: Path,
    topic_id: str,
    alias: str,
    task_n: int,
    cwd: str,
) -> None:
    """queueファイルにspawning write-aheadを記録する（孤児worker対策）。"""
    queue_file = queue_dir / f"queue-t{topic_id}.md"
    now = datetime.now(timezone.utc).isoformat()

    spawning_entry = (
        f"\n## T{task_n} | spawning | spawning\n"
        f"- worker: {alias} / term_ref: (pending) / session: (pending)\n"
        f"- cwd: {cwd}\n"
        f"- spawning: {now}\n"
    )

    queue_file.parent.mkdir(parents=True, exist_ok=True)
    with open(queue_file, "a", encoding="utf-8") as f:
        f.write(spawning_entry)


def _write_task_file(
    task_dir: Path,
    task_n: int,
    alias: str,
    channel: str,
    cwd: str,
    model: str,
    permission: str,
    task_title: str,
    acceptance: str,
    context: str,
    playbook: str,
    timeout_min: int,
    activity_id: int | None,
    topic_id: str | None,
) -> Path:
    """task fileを書き出す。"""
    task_file = task_dir / f"T{task_n}.json"
    task_file.parent.mkdir(parents=True, exist_ok=True)

    task_data = {
        "v": 1,
        "task": f"T{task_n}",
        "alias": alias,
        "channel": channel,
        "cwd": cwd,
        "model": model,
        "permission_mode": permission,
        "title": task_title,
        "acceptance": acceptance,
        "context": context,
        "playbook": playbook,
        "timeout_min": timeout_min,
        "activity_id": activity_id,
        "topic_id": topic_id,
    }

    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(task_data, f, ensure_ascii=False, indent=2)

    return task_file


def _get_adapter_path(terminal: str) -> Path | None:
    """アダプタスクリプトのパスを返す（不在ならNone）。"""
    scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts" / "ow" / "adapters"
    adapter = scripts_dir / f"{terminal}.sh"
    if adapter.exists():
        return adapter
    return None


def ow_spawn_worker(
    alias: str,
    channel: str,
    cwd: str,
    model: str,
    permission: str = "acceptEdits",
    task_title: str = "",
    acceptance: str = "",
    context: str = "",
    playbook: str = "",
    timeout_min: int = 60,
    activity_id: int | None = None,
    topic_id: str | None = None,
    task_n: int = 1,
) -> dict:
    """workerセッションを起動する。

    処理順: queueへspawning write-ahead → task file書き出し → アダプタ呼び出し → 安定ID返却

    Args:
        alias: workerのhandle（例: "w-a"）
        channel: channelコード
        cwd: workerの作業ディレクトリ
        model: 使用モデル（例: "sonnet", "opus"）
        permission: permission_mode（デフォルト: "acceptEdits"）
        task_title: タスクタイトル
        acceptance: 完了条件
        context: タスクコンテキスト
        playbook: プレイブック抜粋
        timeout_min: タイムアウト（分）
        activity_id: 対応するアクティビティID
        topic_id: 対応するトピックID
        task_n: タスク番号（Tn）

    Returns:
        {"term_ref": str, "task_file": str, "spawning": "ok"}
        manualフォールバック時: {"command": str, "manual": True, "task_file": str}
    """
    # relayサーバー確認
    if not ensure_relay_server():
        return {"error": {"code": "RELAY_UNAVAILABLE", "message": "relay server is not available"}}

    # task file書き出し先の決定
    queue_dir = _get_queue_dir()
    if queue_dir is None:
        # OW_QUEUE_DIR未設定の場合は一時的なパスを使用
        queue_dir = Path.home() / ".cc-memory-ow" / "orch"

    task_dir = queue_dir / "tasks"

    # queueへspawning write-ahead（孤児worker対策 D#2395）
    if topic_id is not None:
        _write_queue_spawning(queue_dir, str(topic_id), alias, task_n, cwd)

    # task fileを書き出す
    task_file = _write_task_file(
        task_dir=task_dir,
        task_n=task_n,
        alias=alias,
        channel=channel,
        cwd=cwd,
        model=model,
        permission=permission,
        task_title=task_title,
        acceptance=acceptance,
        context=context,
        playbook=playbook,
        timeout_min=timeout_min,
        activity_id=activity_id,
        topic_id=topic_id,
    )

    # アダプタ起動
    terminal = os.environ.get("OW_TERMINAL", "manual")
    adapter_path = _get_adapter_path(terminal) if terminal != "manual" else None

    worker_cmd = (
        f'env OW_ROLE=worker OW_ALIAS={shlex.quote(alias)} OW_CHANNEL={shlex.quote(channel)} '
        f'OW_TASK_FILE={shlex.quote(str(task_file))} '
        f'claude --model {shlex.quote(model)} --permission-mode {shlex.quote(permission)} '
        f'{shlex.quote(f"workerスキルに従って作業を開始して。task: {task_file}")}'
    )

    if adapter_path is None:
        # manualフォールバック: 起動コマンドを返す
        return {
            "command": worker_cmd,
            "manual": True,
            "task_file": str(task_file),
            "alias": alias,
        }

    # アダプタ呼び出し
    term_ref = str(uuid.uuid4())
    try:
        subprocess.run(
            ["bash", str(adapter_path), "spawn", cwd, worker_cmd, term_ref],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.warning("adapter spawn timed out after 30s")
        return {
            "command": worker_cmd,
            "manual": True,
            "task_file": str(task_file),
            "alias": alias,
            "adapter_error": "adapter spawn timed out",
        }
    except subprocess.CalledProcessError as e:
        logger.warning("adapter spawn failed: %s", e.stderr)
        return {
            "command": worker_cmd,
            "manual": True,
            "task_file": str(task_file),
            "alias": alias,
            "adapter_error": e.stderr,
        }

    return {
        "term_ref": term_ref,
        "task_file": str(task_file),
        "spawning": "ok",
        "alias": alias,
    }


def ow_close_worker(term_ref: str) -> dict:
    """アダプタ経由でworkerセッションをクローズする。

    Args:
        term_ref: 安定ID（iterm2のsession UUID、tmuxのpane ID等）

    Returns:
        {"closed": True} または {"error": ...}
    """
    terminal = os.environ.get("OW_TERMINAL", "manual")
    adapter_path = _get_adapter_path(terminal) if terminal != "manual" else None

    if adapter_path is None:
        return {
            "manual": True,
            "message": f"手動でterm_ref={term_ref}のセッションをクローズしてください",
        }

    try:
        subprocess.run(
            ["bash", str(adapter_path), "close", term_ref],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return {"closed": True, "term_ref": term_ref}
    except subprocess.TimeoutExpired:
        logger.warning("adapter close timed out after 15s")
        return {
            "error": {"code": "ADAPTER_CLOSE_TIMEOUT", "message": "adapter close timed out"},
            "term_ref": term_ref,
        }
    except subprocess.CalledProcessError as e:
        logger.warning("adapter close failed: %s", e.stderr)
        return {
            "error": {"code": "ADAPTER_CLOSE_FAILED", "message": e.stderr},
            "term_ref": term_ref,
        }


# ----------------------------
# T4: ow_status
# ----------------------------


def _parse_queue_file(queue_file: Path) -> list[dict]:
    """queueファイルをパースしてタスク一覧を返す。"""
    if not queue_file.exists():
        return []

    tasks = []
    current_task: dict | None = None

    try:
        content = queue_file.read_text(encoding="utf-8")
    except OSError:
        return []

    for line in content.splitlines():
        line = line.strip()
        # タスクヘッダー: ## T1 | タイトル | status
        if line.startswith("## T") and " | " in line:
            if current_task is not None:
                tasks.append(current_task)
            parts = line.lstrip("# ").split(" | ")
            task_id = parts[0].strip() if len(parts) > 0 else "?"
            task_status = parts[-1].strip() if len(parts) > 2 else "unknown"
            task_title = " | ".join(parts[1:-1]).strip() if len(parts) > 2 else (parts[1].strip() if len(parts) > 1 else "")
            current_task = {
                "task": task_id,
                "title": task_title,
                "status": task_status,
                "worker": None,
                "term_ref": None,
            }
        elif current_task is not None and line.startswith("- worker:"):
            # "- worker: w-a / term_ref: iterm2:xxx / session: <uuid>"
            worker_info = line[len("- worker:"):].strip()
            for part in worker_info.split("/"):
                part = part.strip()
                if part.startswith("term_ref:"):
                    current_task["term_ref"] = part[len("term_ref:"):].strip()
                elif not part.startswith("session:"):
                    current_task["worker"] = part

    if current_task is not None:
        tasks.append(current_task)

    return tasks


def ow_status(channel: str, topic_id: str | None = None) -> dict:
    """queueサマリ＋GetPresence（worker死活）の合成ビュー。

    Args:
        channel: channelコード（presence取得に使用）
        topic_id: queueファイル特定に使用（OW_QUEUE_DIRと組み合わせ）

    Returns:
        {"tasks": [...], "presence": [...], "summary": {...}}
    """
    # presenceを取得
    presence_result = _relay_request("GET", f"/presence?{urllib.parse.urlencode({'channel': channel})}")
    if "error" in presence_result:
        handles = []
    else:
        handles = presence_result.get("handles", [])

    # queueファイルをパース
    tasks: list[dict] = []
    queue_dir = _get_queue_dir()
    if queue_dir is not None and topic_id is not None:
        queue_file = queue_dir / f"queue-t{topic_id}.md"
        tasks = _parse_queue_file(queue_file)
    elif queue_dir is not None:
        # topic_id未指定の場合は存在する全queueファイルを読む
        for queue_file in sorted(queue_dir.glob("queue-t*.md")):
            tasks.extend(_parse_queue_file(queue_file))

    # presenceとqueueの統合
    for task in tasks:
        worker = task.get("worker")
        if worker:
            task["online"] = worker in handles

    # サマリ
    status_counts: dict[str, int] = {}
    for task in tasks:
        s = task.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    return {
        "tasks": tasks,
        "presence": handles,
        "summary": {
            "total_tasks": len(tasks),
            "status_counts": status_counts,
            "online_workers": [h for h in handles if h.startswith("w-")],
        },
    }
