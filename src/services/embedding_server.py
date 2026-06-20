"""Embeddingサーバー: モデル保持 + encode を1プロセスに集約するHTTPサーバー"""
import datetime
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal, Optional

HOST = "localhost"
PORT = 52836
MAX_REQUEST_BYTES = 10 * 1024 * 1024  # 10MB

# shutdown policy パラメータ（env var で上書き可能）
# 既存スタイル（src/config.py）に合わせ default は文字列で渡す。不正値はクラッシュさせて早期発見する。
_TTL_SEC = int(os.environ.get("CC_MEMORY_EMBEDDING_TTL_SEC", "3600"))
_DRAIN_IDLE_SEC = int(os.environ.get("CC_MEMORY_EMBEDDING_DRAIN_IDLE_SEC", "30"))
_DRAIN_DEADLINE_SEC = int(os.environ.get("CC_MEMORY_EMBEDDING_DRAIN_DEADLINE_SEC", "1800"))
_WATCHDOG_INTERVAL_SEC = 10  # watchdog のチェック粒度

MODEL_NAME = "cl-nagoya/ruri-v3-70m"
DOC_PREFIX = "検索文書: "
QUERY_PREFIX = "検索クエリ: "

logger = logging.getLogger("embedding_server")

# グローバル状態
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

    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
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

        _model = SentenceTransformer(MODEL_NAME)
        logger.info(f"Model loaded successfully: {MODEL_NAME}")
    except Exception as e:
        logger.error(f"Model loading failed: {e}")
        sys.exit(1)


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


def _shutdown_server(server: ThreadingHTTPServer) -> None:
    """ThreadingHTTPServer を graceful に止める（別スレッドから呼ぶ前提）。

    serve_forever ループから抜けるには別スレッドから shutdown() を呼ぶ必要があるため、
    watchdog スレッドからの呼び出しを想定する。server_close() は main() の finally で
    呼ばれる。
    """
    server.shutdown()


def _watchdog(server: ThreadingHTTPServer):
    """TTL + drain window + force deadline の 3 段階で graceful shutdown を行う watchdog。

    状態遷移:
    - active → (uptime >= _TTL_SEC) → draining
    - draining → (idle >= _DRAIN_IDLE_SEC) → shutdown（graceful）
    - draining → (drain_age >= _DRAIN_DEADLINE_SEC) → shutdown（force deadline）
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
            idle = now - _last_access_time
            drain_age = now - (_drain_started_at or now)
            if idle >= _DRAIN_IDLE_SEC:
                logger.info(f"graceful shutdown (idle {idle:.1f}s during drain)")
                _shutdown_server(server)
                return
            if drain_age >= _DRAIN_DEADLINE_SEC:
                logger.info(
                    f"force shutdown (drain deadline {drain_age:.1f}s exceeded "
                    f"{_DRAIN_DEADLINE_SEC}s)"
                )
                _shutdown_server(server)
                return


def main():
    _setup_logging()
    _load_model()

    try:
        server = ThreadingHTTPServer((HOST, PORT), EmbeddingHandler)
    except OSError as e:
        logger.error(f"server_bind failed: {e}")
        sys.exit(1)

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
