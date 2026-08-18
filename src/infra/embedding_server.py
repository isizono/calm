"""Embeddingサーバー: モデル保持 + encode を1プロセスに集約するHTTPサーバー"""
import datetime
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from typing import Literal, Optional

from src.env_compat import env_get

HOST = "localhost"
PORT = 52836
MAX_REQUEST_BYTES = 10 * 1024 * 1024  # 10MB

# shutdown policy パラメータ（env var で上書き可能）
# 既存スタイル（src/config.py）に合わせ default は文字列で渡す。不正値はクラッシュさせて早期発見する。
_TTL_SEC = int(env_get("CALM_EMBEDDING_TTL_SEC", "3600"))
_DRAIN_IDLE_SEC = int(env_get("CALM_EMBEDDING_DRAIN_IDLE_SEC", "30"))
_DRAIN_DEADLINE_SEC = int(env_get("CALM_EMBEDDING_DRAIN_DEADLINE_SEC", "1800"))
_WATCHDOG_INTERVAL_SEC = 10  # watchdog のチェック粒度

# ログローテーション設定（env var で上書き可能）
_LOG_MAX_BYTES = int(env_get("CALM_EMBEDDING_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
_LOG_BACKUP_COUNT = int(env_get("CALM_EMBEDDING_LOG_BACKUP_COUNT", "3"))

MODEL_NAME = "cl-nagoya/ruri-v3-70m"
DOC_PREFIX = "検索文書: "
QUERY_PREFIX = "検索クエリ: "

logger = logging.getLogger("embedding_server")

# グローバル状態
#
# Thread safety: 以下のグローバルは ThreadingHTTPServer のリクエストスレッド（_last_access_time
# を書く）と watchdog スレッド（_state / _drain_started_at を書き、_last_access_time を読む）から
# 並行アクセスされる。意図的にロックを取っていない:
#   - Python の GIL により単一の float / 参照代入はアトミックである
#   - 書き手は各変数ごとに 1 スレッドに集約されている（_last_access_time はリクエストハンドラのみ、
#     _state / _drain_started_at は watchdog のみが書く）
#   - 読み取りで多少古い値を見ても shutdown 判断が 1 tick (= _WATCHDOG_INTERVAL_SEC) 遅れるだけで
#     セマンティクスは壊れない
# このコメントは「ロック忘れ」と「意図的なロックレス」を区別するためのもの。
_model = None
_started_at: float = time.time()
_last_access_time: float = time.time()
_drain_started_at: Optional[float] = None
_state: Literal["active", "draining"] = "active"


def _setup_logging():
    """ログを ~/.cache/cc-memory/embedding-server.log に追記する（複数プロセス境界はヘッダー行で識別）。"""
    log_dir = os.path.expanduser("~/.cache/cc-memory")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "embedding-server.log")

    # ローテーション付きで追記する。複数プロセスが同一ファイルに書く構造ではないため
    # （embedding_server は単一インスタンス想定）、RotatingFileHandler でロック競合は発生しない。
    handler = RotatingFileHandler(
        log_path,
        mode="a",
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # プロセス境界の目印を 1 行出力
    started_iso = datetime.datetime.now().astimezone().isoformat()
    logger.info(f"=== PID {os.getpid()} started at {started_iso} ===")


def _load_model():
    """sentence-transformersモデルをロードする。"""
    global _model
    logger.info(f"Model loading started: {MODEL_NAME}")
    logger.info(f"Python executable: {sys.executable}")
    try:
        from sentence_transformers import SentenceTransformer

        # Apple SiliconではMPSが自動選択され、過去にMetal GPUの数十GB級メモリ暴走を
        # 起こしたため、小型モデルはCPU固定とする。
        _model = SentenceTransformer(MODEL_NAME, device="cpu")
        logger.info(f"Model loaded successfully: {MODEL_NAME}")
    except Exception as e:
        logger.error(f"Model loading failed: {e}")
        sys.exit(1)


class EmbeddingHTTPServer(ThreadingHTTPServer):
    """backlog を拡張した ThreadingHTTPServer。

    bind はモデルロード前に行うため（main() 参照）、ロード中（accept 開始前）の
    ヘルスチェック接続が listen backlog に溜まる。既定値 5 では数十秒のロード中に
    枯渇して SYN がドロップされ、クライアント側から「ポート未 bind」と区別が
    つかなくなるため余裕を持たせる。
    """
    request_queue_size = 128


class EmbeddingHandler(BaseHTTPRequestHandler):
    """HTTPリクエストハンドラ"""

    def log_message(self, format, *args):
        """デフォルトのstderrログを抑制し、loggerに転送する。"""
        logger.info(format % args)

    def _send_json(self, status_code: int, data: dict):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_GET(self):
        global _last_access_time
        _last_access_time = time.time()

        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        global _last_access_time
        _last_access_time = time.time()

        if self.path != "/encode":
            self._send_json(404, {"error": "Not found"})
            return

        # リクエストボディをパース
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > MAX_REQUEST_BYTES:
                self._send_json(413, {"error": "Request body too large"})
                return
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json(400, {"error": "Invalid request body"})
            return

        # バリデーション
        texts = data.get("texts")
        prefix_type = data.get("prefix")

        if not isinstance(texts, list) or not texts:
            self._send_json(400, {"error": "texts must be a non-empty list"})
            return
        if prefix_type not in ("document", "query"):
            self._send_json(400, {"error": 'prefix must be "document" or "query"'})
            return

        # prefix付与 + encode
        prefix = DOC_PREFIX if prefix_type == "document" else QUERY_PREFIX
        prefixed_texts = [prefix + t for t in texts]

        try:
            embeddings = _model.encode(prefixed_texts)
            result = [e.tolist() for e in embeddings]
            self._send_json(200, {"embeddings": result})
        except Exception as e:
            logger.error(f"encode failed: {e}")
            self._send_json(500, {"error": "Internal server error"})


def _watchdog(server: ThreadingHTTPServer):
    """TTL + drain window + force deadline の 3 段階で graceful shutdown を行う watchdog。

    状態遷移:
    - active → (uptime >= _TTL_SEC) → draining
    - draining → (idle >= _DRAIN_IDLE_SEC) → shutdown（graceful）
    - draining → (drain_age >= _DRAIN_DEADLINE_SEC) → shutdown（force deadline）

    serve_forever ループから抜けるには別スレッドから server.shutdown() を呼ぶ必要があり、
    本関数は daemon thread として起動される前提。server_close() は main() の finally で呼ばれる。
    """
    global _state, _drain_started_at
    while True:
        time.sleep(_WATCHDOG_INTERVAL_SEC)
        now = time.time()
        if _state == "active":
            uptime = now - _started_at
            if uptime >= _TTL_SEC:
                _state = "draining"
                _drain_started_at = now
                logger.info(
                    f"entering draining mode (uptime {uptime:.1f}s exceeded TTL {_TTL_SEC}s)"
                )
        elif _state == "draining":
            # active → draining の遷移時に必ず _drain_started_at をセットしているため、
            # ここに到達した時点では None ではない。防衛的 fallback ではバグを silent に飲み込む
            # 危険があるので assert で fail-fast する。
            assert _drain_started_at is not None, (
                "_drain_started_at must be set before entering draining state"
            )
            idle = now - _last_access_time
            drain_age = now - _drain_started_at
            if idle >= _DRAIN_IDLE_SEC:
                logger.info(f"graceful shutdown (idle {idle:.1f}s during drain)")
                server.shutdown()
                return
            if drain_age >= _DRAIN_DEADLINE_SEC:
                logger.info(
                    f"force shutdown (drain deadline {drain_age:.1f}s exceeded "
                    f"{_DRAIN_DEADLINE_SEC}s)"
                )
                server.shutdown()
                return


def main():
    _setup_logging()

    # bind をモデルロードより先に行う。多重起動の敗者判定は bind 失敗で行われるため、
    # ロードを先にすると敗者も数百MBのモデルをロードし終えてから退場することになり、
    # 並行 spawn 時にロード分のメモリが spawn 数だけ積み上がる。bind 済み・ロード中の
    # 接続は backlog に溜まり、serve_forever 開始後に処理される（/health はロード完了
    # まで応答しないので、クライアントは起動待ちを継続する）。
    try:
        server = EmbeddingHTTPServer((HOST, PORT), EmbeddingHandler)
    except OSError as e:
        logger.error(f"server_bind failed: {e}")
        sys.exit(1)

    _load_model()

    logger.info(f"Embedding server listening on {HOST}:{PORT}")

    watchdog = threading.Thread(target=_watchdog, args=(server,), daemon=True)
    watchdog.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down: KeyboardInterrupt")
    finally:
        server.server_close()
        logger.info("Server closed")


if __name__ == "__main__":
    main()
