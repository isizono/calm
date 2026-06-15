#!/usr/bin/env python3
"""relay 中継サーバー（SQLite永続化版、cc-memory vendoring）。

エンドポイント:
- POST /create                                   : channel_code 発行
- GET  /stream?channel=CODE&handle=NAME          : SSE購読（接続=presence登録）
- POST /send                                     : メッセージ保存 + 全購読者へブロードキャスト
- GET  /history?channel=CODE[&since=N][&limit=N] : 履歴取得
- GET  /presence?channel=CODE                    : 現在の接続中handle一覧
- GET  /health                                   : 生死＋PROTOCOL_VERSION（自己修復gate用）

メッセージ順序の真実源は ``msg_id`` (AUTOINCREMENT で単調増加)。SSE broadcast の
到達順は厳密に保証しない（受信側は GetHistory + msg_id で冪等突合する設計）。
SQLite 同時書き込みは WAL モード + busy_timeout で吸収する（`_db_connect`）。

標準ライブラリのみ。依存ゼロ（D#2282）。
"""
import json
import os
import queue
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import PROTOCOL_VERSION

PORT = int(os.environ.get("RELAY_PORT", "8765"))
DB_PATH = os.environ.get(
    "RELAY_DB",
    str(Path.home() / ".cc-memory" / "ow" / "relay" / "relay.db"),
)

# channel_code → list of (handle, queue.Queue)
# presence も兼用: 接続中の handle はこのリストに現れる
_subscribers: dict[str, list[tuple[str, queue.Queue]]] = {}
_sub_lock = threading.Lock()

# アイドル削除ジョブ（1日ごとに再スケジュール）
_idle_timer: threading.Timer | None = None
_idle_timer_lock = threading.Lock()

IDLE_SECONDS = 365 * 24 * 3600  # 1年
IDLE_JOB_INTERVAL = 86400  # 1日

# 起動時刻（/healthのuptime計算用）
_started_at: float = time.time()
_started_at_iso: str = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# SQLite ヘルパー
# ---------------------------------------------------------------------------

def _db_connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """スレッドごとに新しい SQLite 接続を開く（check_same_thread 問題回避）。

    ThreadingHTTPServer 配下で複数スレッドが同時に書き込むため、WAL モードと
    busy_timeout を設定して ``sqlite3.OperationalError: database is locked`` を
    回避する。複数 Claude が同一 channel に同時 send するのがこのシステムの常態で
    あり、並行書き込みは想定内のため（D#2285 でハンドルは非検証＝アクセスは
    bridge-connect 経由に限られるが、同時アクセス自体は起こりうる）。
    """
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    """テーブルを作成する（起動時に呼ぶ）。"""
    conn = _db_connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_code     TEXT PRIMARY KEY,
                created_at       TEXT NOT NULL,
                last_activity_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                msg_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_code TEXT NOT NULL REFERENCES channels(channel_code),
                handle       TEXT NOT NULL,
                body         TEXT NOT NULL,
                needs_reply  INTEGER NOT NULL,
                in_reply_to  INTEGER,
                created_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_channel_msg_id
                ON messages(channel_code, msg_id);
        """)
        conn.commit()
    finally:
        conn.close()


def _now_iso() -> str:
    """現在時刻を ISO 8601 文字列で返す（UTC）。"""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# channel CRUD
# ---------------------------------------------------------------------------

def create_channel(db_path: str = DB_PATH) -> str:
    """channel を作成し channel_code を返す。衝突時はリトライ（D#2287）。"""
    conn = _db_connect(db_path)
    try:
        for _ in range(10):
            code = secrets.token_urlsafe(8)
            now = _now_iso()
            try:
                conn.execute(
                    "INSERT INTO channels (channel_code, created_at, last_activity_at) VALUES (?, ?, ?)",
                    (code, now, now),
                )
                conn.commit()
                return code
            except sqlite3.IntegrityError:
                # UNIQUE 制約違反 → リトライ
                continue
        raise RuntimeError("channel_code 発行に10回失敗しました")
    finally:
        conn.close()


def ensure_channel(channel_code: str, db_path: str = DB_PATH) -> str:
    """指定 channel_code が存在しなければ作成して返す（idempotent）。

    既に存在する場合はそのまま channel_code を返す。
    """
    conn = _db_connect(db_path)
    try:
        row = conn.execute(
            "SELECT channel_code FROM channels WHERE channel_code = ?", (channel_code,)
        ).fetchone()
        if row is not None:
            return row["channel_code"]
        now = _now_iso()
        conn.execute(
            "INSERT INTO channels (channel_code, created_at, last_activity_at) VALUES (?, ?, ?)",
            (channel_code, now, now),
        )
        conn.commit()
        return channel_code
    finally:
        conn.close()


def channel_exists(channel_code: str, db_path: str = DB_PATH) -> bool:
    """channel_code が DB に存在するか確認する。"""
    conn = _db_connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM channels WHERE channel_code = ?", (channel_code,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _update_last_activity(channel_code: str, ts: str, conn: sqlite3.Connection) -> None:
    """last_activity_at を更新する（既存の conn を再利用）。"""
    conn.execute(
        "UPDATE channels SET last_activity_at = ? WHERE channel_code = ?",
        (ts, channel_code),
    )


# ---------------------------------------------------------------------------
# メッセージ操作
# ---------------------------------------------------------------------------

def save_message(
    channel_code: str,
    handle: str,
    body: str,
    needs_reply: bool,
    in_reply_to: int | None,
    db_path: str = DB_PATH,
) -> dict:
    """メッセージを保存し、保存されたメッセージ dict を返す。

    in_reply_to が同一 channel 内に存在しない msg_id の場合は ValueError を送出する。
    """
    conn = _db_connect(db_path)
    try:
        # in_reply_to バリデーション
        if in_reply_to is not None:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE msg_id = ? AND channel_code = ?",
                (in_reply_to, channel_code),
            ).fetchone()
            if row is None:
                raise ValueError(f"in_reply_to={in_reply_to} は channel={channel_code} 内に存在しません")

        now = _now_iso()
        cur = conn.execute(
            "INSERT INTO messages (channel_code, handle, body, needs_reply, in_reply_to, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (channel_code, handle, body, 1 if needs_reply else 0, in_reply_to, now),
        )
        msg_id = cur.lastrowid
        _update_last_activity(channel_code, now, conn)
        conn.commit()
        return {
            "msg_id": msg_id,
            "channel_code": channel_code,
            "handle": handle,
            "body": body,
            "needs_reply": needs_reply,
            "in_reply_to": in_reply_to,
            "created_at": now,
        }
    finally:
        conn.close()


def get_history(
    channel_code: str,
    since: int | None = None,
    limit: int | None = None,
    db_path: str = DB_PATH,
) -> list[dict]:
    """メッセージ履歴を返す。

    since 指定時は msg_id > since のメッセージのみ返す（since 自身は含まない）。
    limit 指定時は最大 N 件に制限する。
    """
    conn = _db_connect(db_path)
    try:
        since_val = since if since is not None else 0
        sql = (
            "SELECT msg_id, handle, body, needs_reply, in_reply_to, created_at"
            " FROM messages"
            " WHERE channel_code = ? AND msg_id > ?"
            " ORDER BY msg_id"
        )
        params: list = [channel_code, since_val]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "msg_id": r["msg_id"],
                "handle": r["handle"],
                "body": r["body"],
                "needs_reply": bool(r["needs_reply"]),
                "in_reply_to": r["in_reply_to"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# アイドル削除ジョブ
# ---------------------------------------------------------------------------

def run_idle_cleanup(db_path: str = DB_PATH) -> int:
    """last_activity_at が現在から1年超過した channel を削除する。

    削除件数（channel 数）を返す。
    """
    conn = _db_connect(db_path)
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - IDLE_SECONDS
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        # 削除対象の channel_code を取得
        rows = conn.execute(
            "SELECT channel_code FROM channels WHERE last_activity_at < ?",
            (cutoff_iso,),
        ).fetchall()
        codes = [r["channel_code"] for r in rows]
        for code in codes:
            conn.execute("DELETE FROM messages WHERE channel_code = ?", (code,))
            conn.execute("DELETE FROM channels WHERE channel_code = ?", (code,))
        conn.commit()
        return len(codes)
    finally:
        conn.close()


def _schedule_idle_cleanup(db_path: str = DB_PATH) -> None:
    """アイドル削除ジョブを実行し、1日後に再スケジュールする。"""
    global _idle_timer
    run_idle_cleanup(db_path)
    with _idle_timer_lock:
        _idle_timer = threading.Timer(
            IDLE_JOB_INTERVAL, _schedule_idle_cleanup, kwargs={"db_path": db_path}
        )
        _idle_timer.daemon = True
        _idle_timer.start()


def stop_idle_job() -> None:
    """アイドル削除ジョブのタイマーを停止する（テスト用）。"""
    global _idle_timer
    with _idle_timer_lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None


# ---------------------------------------------------------------------------
# presence / ブロードキャスト
# ---------------------------------------------------------------------------

def get_presence(channel_code: str) -> list[str]:
    """現在 SSE 接続中の handle 一覧を返す（重複除去・順序不定）。"""
    with _sub_lock:
        entries = _subscribers.get(channel_code, [])
        # 同一 handle が複数接続していても1件として扱う
        seen: dict[str, bool] = {}
        result = []
        for handle, _ in entries:
            if handle not in seen:
                seen[handle] = True
                result.append(handle)
        return result


def _broadcast(channel_code: str, sender_handle: str, msg: dict) -> None:
    """同一 channel の購読者（送信者と同一 handle を除く）にメッセージを配信する（D#2286）。

    SSE ペイロードは msg_id / body / handle / created_at の4フィールドに絞る。
    受信側が body フィールド有無を許容する後方互換設計を前提とする。
    """
    payload = json.dumps(
        {
            "msg_id": msg["msg_id"],
            "body": msg["body"],
            "handle": msg["handle"],
            "created_at": msg["created_at"],
        },
        ensure_ascii=False,
    )
    with _sub_lock:
        entries = list(_subscribers.get(channel_code, []))
    for handle, q in entries:
        if handle == sender_handle:
            # 送信者自身へはエコーしない（D#2286）
            continue
        q.put(payload)


# ---------------------------------------------------------------------------
# HTTP ハンドラ
# ---------------------------------------------------------------------------

def _parse_path(path: str):
    """パスとクエリパラメータをパースして (route, params) を返す。"""
    parsed = urlparse(path)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    return parsed.path, params


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # アクセスログ抑制
        pass

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def do_GET(self):
        route, params = _parse_path(self.path)

        if route == "/stream":
            self._handle_stream(params)
        elif route == "/history":
            self._handle_history(params)
        elif route == "/presence":
            self._handle_presence(params)
        elif route == "/health":
            self._handle_health()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_health(self) -> None:
        """ヘルスチェック。protocol_version を含む dict を返す。

        ow_service側の `_get_relay_health` がこのレスポンスを叩いて
        「相手のrelayが現在のowコードと互換か」を判定する。版不一致なら
        kill+restartで自己修復する（acceptance③）。
        """
        self._send_json(200, {
            "status": "ok",
            "protocol_version": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "db_path": str(Path(DB_PATH).resolve()),
            "started_at": _started_at_iso,
            "uptime_sec": int(time.time() - _started_at),
        })

    def _handle_stream(self, params: dict) -> None:
        """SSE ストリーム（接続 = presence 登録）。"""
        channel_code = params.get("channel")
        handle = params.get("handle")

        if not channel_code or not handle:
            self._send_json(400, {"error": "channel と handle は必須です"})
            return

        if not channel_exists(channel_code, DB_PATH):
            self._send_json(404, {"error": "channel が見つかりません"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q: queue.Queue = queue.Queue()
        with _sub_lock:
            if channel_code not in _subscribers:
                _subscribers[channel_code] = []
            _subscribers[channel_code].append((handle, q))

        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                msg = q.get()
                self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sub_lock:
                entries = _subscribers.get(channel_code, [])
                try:
                    entries.remove((handle, q))
                except ValueError:
                    pass

    def _handle_history(self, params: dict) -> None:
        """履歴取得。"""
        channel_code = params.get("channel")
        if not channel_code:
            self._send_json(400, {"error": "channel は必須です"})
            return

        if not channel_exists(channel_code, DB_PATH):
            self._send_json(404, {"error": "channel が見つかりません"})
            return

        since_str = params.get("since")
        limit_str = params.get("limit")

        since: int | None = None
        limit: int | None = None

        if since_str is not None:
            try:
                since = int(since_str)
            except ValueError:
                self._send_json(400, {"error": "since は整数で指定してください"})
                return

        if limit_str is not None:
            try:
                limit = int(limit_str)
                if limit <= 0:
                    raise ValueError
            except ValueError:
                self._send_json(400, {"error": "limit は正の整数で指定してください"})
                return

        messages = get_history(channel_code, since=since, limit=limit, db_path=DB_PATH)
        self._send_json(200, {"messages": messages})

    def _handle_presence(self, params: dict) -> None:
        """presence 取得。"""
        channel_code = params.get("channel")
        if not channel_code:
            self._send_json(400, {"error": "channel は必須です"})
            return

        if not channel_exists(channel_code, DB_PATH):
            self._send_json(404, {"error": "channel が見つかりません"})
            return

        handles = get_presence(channel_code)
        self._send_json(200, {"handles": handles})

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------

    def do_POST(self):
        route, params = _parse_path(self.path)

        if route == "/create":
            self._handle_create()
        elif route == "/send":
            self._handle_send()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_create(self) -> None:
        """channel 作成。ボディで channel_code を指定した場合はその値で作成（idempotent）。"""
        length = int(self.headers.get("Content-Length", 0))
        channel_code = None
        if length > 0:
            raw = self.rfile.read(length).decode("utf-8")
            try:
                data = json.loads(raw)
                channel_code = data.get("channel_code") or None
            except (json.JSONDecodeError, AttributeError):
                pass

        if channel_code:
            code = ensure_channel(channel_code, DB_PATH)
        else:
            code = create_channel(DB_PATH)
        self._send_json(200, {"channel_code": code})

    def _handle_send(self) -> None:
        """メッセージ送信・保存・ブロードキャスト。"""
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "JSON パースエラー"})
            return

        channel_code = data.get("channel")
        handle = data.get("handle")
        body = data.get("body")
        needs_reply = data.get("needs_reply", False)
        in_reply_to = data.get("in_reply_to")  # None or int

        if not channel_code or not handle or body is None:
            self._send_json(400, {"error": "channel, handle, body は必須です"})
            return

        if not channel_exists(channel_code, DB_PATH):
            self._send_json(404, {"error": "channel が見つかりません"})
            return

        try:
            msg = save_message(
                channel_code=channel_code,
                handle=handle,
                body=body,
                needs_reply=bool(needs_reply),
                in_reply_to=in_reply_to,
                db_path=DB_PATH,
            )
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return

        _broadcast(channel_code, handle, msg)
        self._send_json(200, {"msg_id": msg["msg_id"]})

    # ------------------------------------------------------------------
    # ユーティリティ
    # ------------------------------------------------------------------

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def main(db_path: str = DB_PATH):
    # DBファイルの親ディレクトリを保証する（固定パス~/.cc-memory/ow/relay/配下）
    Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    _schedule_idle_cleanup(db_path)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(
        f"relay on http://127.0.0.1:{PORT} (protocol_version={PROTOCOL_VERSION})",
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        stop_idle_job()


if __name__ == "__main__":
    main()
