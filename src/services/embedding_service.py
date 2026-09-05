"""Embeddingサービス: embedding_serverへのHTTPクライアント + vec_index操作"""
import json
import logging
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

from sqlite_vec import serialize_float32

from src.db import execute_query, get_connection
from src.env_compat import env_get
from src.infra.lock_file import is_port_listening

logger = logging.getLogger(__name__)

# サーバー接続設定
PORT = 52836
SERVER_URL = f"http://localhost:{PORT}"


def _resolve_project_root() -> str:
    """embedding_server を起動する cwd を決定する。

    優先順位:
      1. 環境変数 ``CALM_PROJECT_ROOT``
      2. ``git rev-parse --git-common-dir`` の親ディレクトリ（worktree 内からでも main repo を返す）
      3. 上記いずれも失敗した場合は ``RuntimeError`` を raise（黙って ``__file__`` fallback はしない）
    """
    # 1. env var override
    env = env_get("CALM_PROJECT_ROOT")
    if env:
        return str(Path(env).resolve())

    # 2. git common-dir based (worktree からでも main repo を返す)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent,
        )
        common_dir = Path(result.stdout.strip())
        # common-dir は relative の可能性があるので resolve
        if not common_dir.is_absolute():
            common_dir = (Path(__file__).parent / common_dir).resolve()
        # main repo root = .git の親
        return str(common_dir.parent.resolve())
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(
            "Failed to resolve project root: set CALM_PROJECT_ROOT "
            f"or run from within a git repo. cause: {e}"
        )


# グローバル状態
#
# Thread safety: `_project_root_cache` は複数スレッドから読み書きされうるが、
# GIL によりアトミックな代入であり、_resolve_project_root が冪等な解決のため
# 意図的にロックを取っていない。
# `_server_initialized` / `_backfill_done` / `_backfill_started` / `_backfill_thread`
# は `_init_lock`（後述）で保護する。バックフィルの多重起動を防ぐため。
_server_initialized = False
_backfill_done = False
_project_root_cache: Optional[str] = None

# _ensure_initialized 全体を保護するロック。ロック無しだと、サーバー復帰直後の
# 並行呼び出しが _server_initialized をまだ見ていない状態で複数スレッド同時に
# バックフィルを開始しうる（重複実行によるDB書込競合・embedding_server への
# 二重リクエスト）。
_init_lock = threading.Lock()

# バックフィルは一度だけdaemon threadで起動する。_backfill_started はスレッド
# 起動済みかどうかのフラグ（_init_lock保持中のみ読み書き）、_backfill_thread は
# テストからjoinできるよう起動したthreadを保持する（実行時は参照しなくてよい）。
_backfill_started = False
_backfill_thread: Optional[threading.Thread] = None

# spawn 直列化ロック。FastMCP は sync ツールを threadpool で並行実行するため、
# ロックなしだとサーバー停止中の並行呼び出しが全スレッド分の embedding_server を
# spawn する（各子プロセスがモデルロード分のメモリを確保する）。
_spawn_lock = threading.Lock()

# 起動失敗後の spawn 再試行クールダウン。モデルロードが恒久的に失敗する環境
# （ネットワーク遮断等）で encode 呼び出しごとに spawn→即死ループになるのを防ぐ。
# 子プロセスは失敗までに sentence_transformers の import 分のメモリを毎回確保する
# ため、失敗直後の再 spawn は許可しない。_spawn_lock 保持中のみ読み書きする。
_SPAWN_RETRY_COOLDOWN_SEC = 30.0
_last_spawn_failed_at: Optional[float] = None


def _read_positive_int_env(name: str, default: int) -> int:
    """envから正の整数を読む。未設定・無効値・0以下はdefaultにフォールバックする。"""
    raw = env_get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"Invalid {name}={raw!r}, falling back to default {default}")
        return default
    if value <= 0:
        logger.warning(f"{name} must be > 0, got {value}, falling back to default {default}")
        return default
    return value


# backfill_embeddings のチャンク分割サイズ。数百〜千件超の未embeddingレコードを
# 1リクエストにまとめて送ると、_encode_batch の60秒固定timeoutをCPU推論が超え、
# 結果が破棄される一方でサーバー側スレッドは計算を続け「見捨てられたスレッド」が
# 積み上がる。文字数・件数どちらか先に達した方でチャンクを区切る。
BACKFILL_CHAR_BUDGET = _read_positive_int_env("CALM_EMBEDDING_BACKFILL_CHAR_BUDGET", 48_000)
BACKFILL_MAX_ITEMS = _read_positive_int_env("CALM_EMBEDDING_BACKFILL_MAX_ITEMS", 64)

# _encode_batch に渡す1テキストあたりの最大文字数。埋め込みモデル自体のtoken上限
# 対策に加え、巨大な1件がbackfillの文字数予算を単独で食い潰すのを防ぐ。
TEXT_MAX_CHARS = _read_positive_int_env("CALM_EMBEDDING_TEXT_MAX_CHARS", 8_000)


def _get_project_root() -> str:
    """`_resolve_project_root()` の lazy + cache wrapper。

    モジュール import 時に subprocess を起動する副作用を避け、最初に
    `_start_server()` が呼ばれる時点で解決する。一度解決した値はプロセス内で再利用する。
    """
    global _project_root_cache
    if _project_root_cache is None:
        _project_root_cache = _resolve_project_root()
    return _project_root_cache


def _is_server_running() -> bool:
    """GET /health でサーバーの生存確認を行う。"""
    try:
        req = urllib.request.Request(f"{SERVER_URL}/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_server() -> Optional[subprocess.Popen]:
    """embedding_serverをdetachedプロセスとして起動する。成功でPopen、失敗でNone。

    `-m src.infra.embedding_server` のモジュール実行形式で起動する（launcher.py の
    `_start_http_server()` と同じ形式）。ファイルパスを直接実行する形式
    （`[sys.executable, server_path]`）だと sys.path[0] が embedding_server.py
    自身のディレクトリになり、内部の `from src.xxx import ...` が
    `ModuleNotFoundError` でクラッシュする。
    """
    try:
        cwd = _get_project_root()
    except (RuntimeError, OSError) as e:
        # project_root が解決できなければ起動も不可能。例外を握って None で返す
        # （呼び出し側 `_ensure_initialized` は False を graceful degradation として扱う）。
        logger.warning(f"Failed to resolve project root for embedding server: {e}")
        return None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "src.infra.embedding_server"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
        )
    except OSError as e:
        logger.warning(f"Failed to start embedding server: {e}")
        return None
    logger.info("Embedding server process started")
    return proc


def _ensure_server_running() -> bool:
    """ヘルスチェック→起動→待機のフロー。成功でTrue、タイムアウトでFalse。

    spawn は _spawn_lock でプロセス内直列化する。ロック取得後の再チェックで
    先行スレッドが起動済みなら spawn しない。
    """
    global _last_spawn_failed_at
    if _is_server_running():
        return True
    with _spawn_lock:
        # ロック待ちの間に別スレッドが起動を完了しているケース
        if _is_server_running():
            return True
        if (
            _last_spawn_failed_at is not None
            and time.time() - _last_spawn_failed_at < _SPAWN_RETRY_COOLDOWN_SEC
        ):
            return False
        proc = _start_server()
        if proc is None:
            _last_spawn_failed_at = time.time()
            return False
        # 最大30秒待機（0.5秒間隔 × 60回）
        for _ in range(60):
            time.sleep(0.5)
            if _is_server_running():
                logger.info("Embedding server is ready")
                _last_spawn_failed_at = None
                return True
            if proc.poll() is not None:
                # 子が終了済み。別プロセス起点のサーバーにbind負けした直後なら
                # health が通るはずなので、最後にもう一度だけ確認してから諦める
                if _is_server_running():
                    logger.info("Embedding server is ready")
                    _last_spawn_failed_at = None
                    return True
                logger.warning(
                    f"Embedding server exited early (returncode={proc.returncode})"
                )
                _last_spawn_failed_at = time.time()
                return False
        # タイムアウト。bind 済み（= ロード進行中で、完了すれば応答する）なら生かし、
        # bind 前に固まっている子は回収する。放置すると stdout/stderr が DEVNULL の
        # 不可視プロセスとしてモデルロード分のメモリを抱えたまま残留するため。
        if is_port_listening(PORT):
            logger.warning(
                "Embedding server not ready within 30 seconds (port bound, still loading)"
            )
        else:
            logger.warning(
                "Embedding server failed to start within 30 seconds, terminating child"
            )
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        _last_spawn_failed_at = time.time()
        return False


def _encode_batch(texts: list[str], prefix: str) -> Optional[list[list[float]]]:
    """POST /encode にバッチリクエストを送信する。

    各テキストは `TEXT_MAX_CHARS` 文字に切り詰めてから送る。日本語テキストは
    `ensure_ascii=False` で送信する（デフォルトのTrueだと `\\uXXXX` 展開で
    リクエストサイズが肥大化し、embedding_server側のサイズ上限に当たりうるため）。

    Args:
        texts: エンコードするテキストのリスト（prefix付与はサーバー側で行う）
        prefix: "document" or "query"

    Returns:
        embeddingのリスト、失敗時はNone
    """
    try:
        truncated = [t[:TEXT_MAX_CHARS] for t in texts]
        data = json.dumps(
            {"texts": truncated, "prefix": prefix}, ensure_ascii=False
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{SERVER_URL}/encode",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            return result["embeddings"]
    except Exception as e:
        logger.warning(f"encode_batch failed: {e}")
        global _server_initialized
        _server_initialized = False
        return None


def _run_backfill() -> None:
    """バックグラウンドthreadで一度だけ実行されるバックフィル本体。

    _ensure_initialized から daemon thread に切り出して呼ばれる。呼び出し元の
    encode_document/encode_query 等のリクエスト経路をバックフィル完了までブロック
    させないため。backfill_embeddings/backfill_topic_embeddings は内部で例外を
    握りつぶし整数を返す契約のためここでは再送出しないが、想定外の例外で
    `_backfill_done` の更新が漏れないよう finally で確実に立てる。
    """
    global _backfill_done
    try:
        backfill_embeddings()
        backfill_topic_embeddings()
    finally:
        _backfill_done = True


def _ensure_initialized() -> bool:
    """サーバー起動確認を行い、バックフィルを一度だけdaemon threadで起動する。

    `_init_lock` で全体を保護する。ロック無しだと、サーバー復帰直後の並行呼び出しが
    `_server_initialized` 更新前に互いをすり抜け、バックフィルを多重起動しうる。
    バックフィル自体は起動するだけでこの関数の戻りを待たせない（同期実行しない）。
    """
    global _server_initialized, _backfill_started, _backfill_thread
    with _init_lock:
        if _server_initialized:
            return True
        running = _ensure_server_running()
        if running:
            _server_initialized = True
            if not _backfill_done and not _backfill_started:
                _backfill_started = True
                _backfill_thread = threading.Thread(target=_run_backfill, daemon=True)
                _backfill_thread.start()
        return running


def build_embedding_text(*fields: Optional[str]) -> str:
    """embeddingテキストを構築する。None/空文字列は除外してスペース結合。"""
    return " ".join(f for f in fields if f)


def encode_document(text: str) -> Optional[list[float]]:
    """ドキュメント用embedding生成。"""
    if not _ensure_initialized():
        return None
    result = _encode_batch([text], "document")
    if result is None:
        return None
    return result[0]


def encode_query(text: str) -> Optional[list[float]]:
    """クエリ用embedding生成。"""
    if not _ensure_initialized():
        return None
    result = _encode_batch([text], "query")
    if result is None:
        return None
    return result[0]


def encode_queries(texts: list[str]) -> Optional[list[list[float]]]:
    """クエリ用embeddingをバッチ生成する。複数テキストを1回のHTTPリクエストにまとめる。"""
    if not texts:
        return []
    if not _ensure_initialized():
        return None
    return _encode_batch(texts, "query")


def generate_and_store_embedding(source_type: str, source_id: int, text: str) -> Optional[list[float]]:
    """search_indexからIDを取得してembeddingを生成・保存する。失敗してもraiseしない。

    Returns:
        生成したembeddingベクトル。失敗時はNone。
    """
    if not text:
        return None
    try:
        rows = execute_query(
            "SELECT id FROM search_index WHERE source_type = ? AND source_id = ?",
            (source_type, source_id),
        )
        if rows:
            search_index_id = rows[0]["id"]
            embedding = encode_document(text)
            if embedding is not None:
                existing = execute_query(
                    "SELECT rowid FROM vec_index WHERE rowid = ?",
                    (search_index_id,),
                )
                if existing:
                    update_embedding(search_index_id, embedding)
                else:
                    insert_embedding(search_index_id, embedding)
                return embedding
    except Exception as e:
        logger.warning(f"Failed to generate embedding for {source_type} {source_id}: {e}")
    return None


def _insert_embedding_row(conn, search_index_id: int, embedding: list[float]) -> None:
    """vec_indexに1行UPSERT（DELETE+INSERT）する（コミットは呼び出し側の責任）。"""
    blob = serialize_float32(embedding)
    conn.execute("DELETE FROM vec_index WHERE rowid = ?", (search_index_id,))
    conn.execute(
        "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
        (search_index_id, blob),
    )


def insert_embedding(search_index_id: int, embedding: list[float]) -> None:
    """vec_indexにembeddingをINSERTする。"""
    conn = get_connection()
    try:
        _insert_embedding_row(conn, search_index_id, embedding)
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to insert embedding for search_index_id={search_index_id}: {e}")
    finally:
        conn.close()


def update_embedding(search_index_id: int, embedding: list[float]) -> None:
    """vec_indexのembeddingを更新する（DELETE+INSERT）。"""
    conn = get_connection()
    try:
        _insert_embedding_row(conn, search_index_id, embedding)
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to update embedding for search_index_id={search_index_id}: {e}")
    finally:
        conn.close()


def delete_embedding_with_conn(conn, search_index_id: int) -> None:
    """vec_indexから1行削除する（コミットは呼び出し側の責任）。

    vec0仮想テーブルは外部キー制約を持てないため、search_indexのレコード削除と
    同じトランザクション内でこの関数を呼び、孤児レコードを残さないようにする。
    """
    conn.execute("DELETE FROM vec_index WHERE rowid = ?", (search_index_id,))


_ENTITY_TEXT_QUERIES = {
    "topic": (
        "SELECT title, description FROM discussion_topics WHERE id = ?",
        ("title", "description"),
    ),
    "decision": (
        "SELECT decision, reason FROM decisions WHERE id = ?",
        ("decision", "reason"),
    ),
    "activity": (
        "SELECT title, description FROM activities WHERE id = ?",
        ("title", "description"),
    ),
    "log": (
        "SELECT title, content FROM discussion_logs WHERE id = ?",
        ("title", "content"),
    ),
    "material": (
        "SELECT title, content FROM materials WHERE id = ?",
        ("title", "content"),
    ),
}


def regenerate_embedding(source_type: str, source_id: int) -> None:
    """エンティティのembeddingをタグ含有テキストで再生成する。

    タグ変更時に呼び出される。失敗してもraiseしない。
    """
    if source_type not in _ENTITY_TEXT_QUERIES:
        return
    try:
        conn = get_connection()
        try:
            query_def = _ENTITY_TEXT_QUERIES[source_type]
            row = conn.execute(query_def[0], (source_id,)).fetchone()
            if not row:
                return
            field1 = row[query_def[1][0]]
            field2 = row[query_def[1][1]]
            tag_text = _get_entity_tag_text(conn, source_type, source_id)
            text = build_embedding_text(field1, field2, tag_text)
            if text:
                generate_and_store_embedding(source_type, source_id, text)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Failed to regenerate embedding for {source_type} {source_id}: {e}")


def _get_entity_tag_text(conn, source_type: str, source_id: int) -> str:
    """エンティティに紐づくタグ文字列をスペース結合で返す（embedding生成・再生成・backfill共通）。"""
    from src.services.tag_service import get_entity_tags, get_effective_tags

    if source_type == "topic":
        tags = get_entity_tags(conn, "topic_tags", "topic_id", source_id)
    elif source_type == "activity":
        tags = get_entity_tags(conn, "activity_tags", "activity_id", source_id)
    elif source_type == "decision":
        tags = get_effective_tags(conn, "decision", source_id)
    elif source_type == "log":
        tags = get_effective_tags(conn, "log", source_id)
    elif source_type == "material":
        tags = get_entity_tags(conn, "material_tags", "material_id", source_id)
    else:
        tags = []
    return " ".join(tags) if tags else ""


def _chunk_backfill_items(
    items: list[tuple[int, str]],
) -> list[list[tuple[int, str]]]:
    """(search_index_id, text) のリストを文字数予算・件数どちらか先に達した方で分割する。

    どちらの上限も超えていない先頭1件は必ずチャンクへ入れる（1件のtextが単独で
    BACKFILL_CHAR_BUDGETを超える場合でも進行が止まらないようにするため）。
    """
    chunks: list[list[tuple[int, str]]] = []
    idx = 0
    while idx < len(items):
        chunk: list[tuple[int, str]] = []
        char_count = 0
        while idx < len(items) and len(chunk) < BACKFILL_MAX_ITEMS:
            search_index_id, text = items[idx]
            if chunk and char_count + len(text) > BACKFILL_CHAR_BUDGET:
                break
            chunk.append((search_index_id, text))
            char_count += len(text)
            idx += 1
        chunks.append(chunk)
    return chunks


def backfill_embeddings() -> int:
    """search_indexにあってvec_indexにないレコードのembeddingを小分けに生成する。

    種別(topic/decision/activity/log/material)ごとに、文字数予算
    (BACKFILL_CHAR_BUDGET)・件数(BACKFILL_MAX_ITEMS)どちらか先に達した方でチャンクを
    区切り、チャンクごとに encode → insert → commit する。数百〜千件超を1リクエストに
    まとめて送ると _encode_batch の60秒固定timeoutをCPU推論が超え結果が破棄される上、
    embedding_server側のスレッドは切断を知らず計算を続け「見捨てられたスレッド」が
    積み上がるため、これを避ける。途中のチャンクが失敗した種別は、サーバーダウンの
    可能性が高く同一種別内の再試行は無駄なため、残りのチャンクを諦めて次の種別へ進む。
    それまでにcommit済みの成果は失われない。

    Returns: 生成したembedding数
    """
    if not _is_server_running():
        return 0

    # リソースタイプごとのクエリ（バッチ推論のためにグループ化）
    type_queries = {
        "topic": """
            SELECT si.id, si.source_id, dt.title, dt.description
            FROM search_index si
            INNER JOIN discussion_topics dt ON si.source_id = dt.id
            LEFT JOIN vec_index vi ON si.id = vi.rowid
            WHERE si.source_type = 'topic' AND vi.rowid IS NULL
        """,
        "decision": """
            SELECT si.id, si.source_id, d.decision, d.reason
            FROM search_index si
            INNER JOIN decisions d ON si.source_id = d.id
            LEFT JOIN vec_index vi ON si.id = vi.rowid
            WHERE si.source_type = 'decision' AND vi.rowid IS NULL
        """,
        "activity": """
            SELECT si.id, si.source_id, a.title, a.description
            FROM search_index si
            INNER JOIN activities a ON si.source_id = a.id
            LEFT JOIN vec_index vi ON si.id = vi.rowid
            WHERE si.source_type = 'activity' AND vi.rowid IS NULL
        """,
        "log": """
            SELECT si.id, si.source_id, dl.title, dl.content
            FROM search_index si
            INNER JOIN discussion_logs dl ON si.source_id = dl.id
            LEFT JOIN vec_index vi ON si.id = vi.rowid
            WHERE si.source_type = 'log' AND vi.rowid IS NULL
        """,
        "material": """
            SELECT si.id, si.source_id, m.title, m.content
            FROM search_index si
            INNER JOIN materials m ON si.source_id = m.id
            LEFT JOIN vec_index vi ON si.id = vi.rowid
            WHERE si.source_type = 'material' AND vi.rowid IS NULL
        """,
    }

    conn = get_connection()
    try:
        total = 0
        for source_type, query in type_queries.items():
            rows = conn.execute(query).fetchall()
            if not rows:
                continue

            items: list[tuple[int, str]] = []
            for row in rows:
                tag_text = _get_entity_tag_text(conn, source_type, row[1])
                text = build_embedding_text(row[2], row[3], tag_text)
                if text:
                    items.append((row[0], text))  # prefix付与はサーバー側で行う

            if not items:
                continue

            for chunk in _chunk_backfill_items(items):
                chunk_ids = [search_index_id for search_index_id, _ in chunk]
                chunk_texts = [text for _, text in chunk]
                try:
                    embeddings = _encode_batch(chunk_texts, "document")
                except Exception as e:
                    logger.warning(f"Failed to backfill {source_type} embeddings: {e}")
                    embeddings = None
                if embeddings is None:
                    logger.warning(
                        f"Backfill chunk failed for {source_type} "
                        f"({len(chunk_ids)} items); giving up on remaining chunks for this type"
                    )
                    break
                for search_index_id, embedding in zip(chunk_ids, embeddings):
                    _insert_embedding_row(conn, search_index_id, embedding)
                    total += 1
                conn.commit()

        logger.info(f"Backfilled {total} embeddings")
        return total
    except Exception as e:
        logger.warning(f"Embedding backfill failed: {e}")
        return total
    finally:
        conn.close()


# ========================================
# Tag embedding ヘルパー
# ========================================


def _insert_tag_embedding_row(conn, tag_id: int, embedding: list[float]) -> None:
    """tag_vecに1行UPSERT（DELETE+INSERT）する（コミットは呼び出し側の責任）。"""
    blob = serialize_float32(embedding)
    conn.execute("DELETE FROM tag_vec WHERE rowid = ?", (tag_id,))
    conn.execute(
        "INSERT INTO tag_vec(rowid, embedding) VALUES (?, ?)",
        (tag_id, blob),
    )


def insert_tag_embedding(tag_id: int, embedding: list[float]) -> None:
    """tag_vecにembeddingをINSERTする。"""
    conn = get_connection()
    try:
        _insert_tag_embedding_row(conn, tag_id, embedding)
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to insert tag embedding for tag_id={tag_id}: {e}")
    finally:
        conn.close()


def generate_and_store_tag_embedding(tag_id: int, tag_name: str) -> None:
    """タグ名からembeddingを生成しtag_vecに格納する。

    サーバーダウン時は何もしない（graceful degradation）。
    """
    if not tag_name:
        return
    try:
        embedding = encode_document(tag_name)
        if embedding is not None:
            insert_tag_embedding(tag_id, embedding)
    except Exception as e:
        logger.warning(f"Failed to generate tag embedding for tag_id={tag_id}: {e}")


def search_similar_tags(query_text: str, k: int = 10) -> list[tuple[int, float]]:
    """tag_vecでKNN検索し、(tag_id, distance)のリストを返す。

    サーバーダウン時は空リストを返す。
    """
    try:
        query_embedding = encode_query(query_text)
        if query_embedding is None:
            return []

        blob = serialize_float32(query_embedding)
        rows = execute_query(
            "SELECT rowid, distance FROM tag_vec WHERE embedding MATCH ? AND k = ?",
            (blob, k),
        )
        return [(row["rowid"], row["distance"]) for row in rows]
    except Exception as e:
        logger.warning(f"Tag similarity search failed: {e}")
        return []


def backfill_tag_embeddings() -> int:
    """tag_vecが空のタグにembeddingを一括生成する。

    Returns: 生成したembedding数
    """
    if not _is_server_running():
        return 0

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT t.id, t.name
            FROM tags t
            LEFT JOIN tag_vec tv ON tv.rowid = t.id
            WHERE tv.rowid IS NULL
            """
        ).fetchall()

        if not rows:
            return 0

        ids = [row["id"] for row in rows]
        texts = [row["name"] for row in rows]

        try:
            embeddings = _encode_batch(texts, "document")
            if embeddings is None:
                return 0
            total = 0
            for tag_id, embedding in zip(ids, embeddings):
                _insert_tag_embedding_row(conn, tag_id, embedding)
                total += 1
            conn.commit()
            logger.info(f"Backfilled {total} tag embeddings")
            return total
        except Exception as e:
            logger.warning(f"Failed to backfill tag embeddings: {e}")
            return 0

    except Exception as e:
        logger.warning(f"Tag embedding backfill failed: {e}")
        return 0
    finally:
        conn.close()


# ========================================
# Topic embedding ヘルパー
#
# topic_vec は distance_metric=cosine で作成される（migration 0049）。同じく非正規化
# embedding を格納する vec_index（0005）と tag_vec（0009）は vec0 既定の L2 のままで、
# topic_vec の distance とはスケールが異なり直接比較できない。topic_vec の近傍距離に
# 閾値を掛ける際は L2 前提の既存しきい値（QE_DISTANCE_THRESHOLD 等）を流用しないこと。
# ========================================


def insert_topic_embedding_with_conn(conn, topic_id: int, embedding: list[float]) -> None:
    """呼び出し側の conn で topic_vec に1行UPSERT（DELETE+INSERT）する（コミットは呼び出し側の責任）。

    add_topic が既に生成した embedding をそのまま渡す想定であり、ここでは再エンコードしない。
    リクエストパス上で新規コネクションを開かないため、sqlite-vec 拡張の再ロードも発生しない。
    """
    blob = serialize_float32(embedding)
    conn.execute("DELETE FROM topic_vec WHERE rowid = ?", (topic_id,))
    conn.execute(
        "INSERT INTO topic_vec(rowid, embedding) VALUES (?, ?)",
        (topic_id, blob),
    )


def delete_topic_embedding_with_conn(conn, topic_id: int) -> None:
    """topic_vecから1行削除する（コミットは呼び出し側の責任）。

    vec0仮想テーブルは外部キー制約を持てないため、topic削除処理を実装する際は
    同じトランザクション内でこの関数を呼び、孤児レコードを残さないようにする。
    """
    conn.execute("DELETE FROM topic_vec WHERE rowid = ?", (topic_id,))


def backfill_topic_embeddings() -> int:
    """topic_vecにembeddingが無いtopicへ、vec_index格納済みのembeddingを複製する。

    add_topic時に生成されvec_indexへ格納済みのembeddingをsearch_index経由で
    引き当てて複製するだけであり、再エンコードは行わない。vec_indexにも
    embeddingが無いtopic（embeddingサーバー停止中に作成された等）は対象外となる。
    その場合はbackfill_embeddings()でvec_indexが埋まった後の呼び出しで拾われる。

    embeddingサーバー未起動時は何もせず0を返す（backfill_embeddings /
    backfill_tag_embeddings と揃えたガード）。

    Returns: 複製したembedding数
    """
    if not _is_server_running():
        return 0

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT dt.id AS topic_id, vi.embedding AS embedding
            FROM discussion_topics dt
            INNER JOIN search_index si ON si.source_type = 'topic' AND si.source_id = dt.id
            INNER JOIN vec_index vi ON vi.rowid = si.id
            LEFT JOIN topic_vec tv ON tv.rowid = dt.id
            WHERE tv.rowid IS NULL
            """
        ).fetchall()

        if not rows:
            return 0

        total = 0
        for row in rows:
            conn.execute(
                "INSERT INTO topic_vec(rowid, embedding) VALUES (?, ?)",
                (row["topic_id"], row["embedding"]),
            )
            total += 1
        conn.commit()
        logger.info(f"Backfilled {total} topic embeddings")
        return total
    except Exception as e:
        logger.warning(f"Topic embedding backfill failed: {e}")
        return 0
    finally:
        conn.close()


# ========================================
# Ask embedding ヘルパー
#
# ask_vec は topic_vec と同じくask専用のvec0仮想テーブル（distance_metric=cosine、
# migration 0062）。asksはsearch_index/vec_indexに参加しない（v1では検索・タグ・
# リレーションの対象外）ため、topic側のようなgenerate_and_store_embedding経由
# ではなく、ここでencode_documentを直接呼んでask_vecにのみ格納する。
# ========================================


def insert_ask_embedding_with_conn(conn, ask_id: int, embedding: list[float]) -> None:
    """呼び出し側のconnでask_vecに1行UPSERT（DELETE+INSERT）する（コミットは呼び出し側の責任）。"""
    blob = serialize_float32(embedding)
    conn.execute("DELETE FROM ask_vec WHERE rowid = ?", (ask_id,))
    conn.execute(
        "INSERT INTO ask_vec(rowid, embedding) VALUES (?, ?)",
        (ask_id, blob),
    )


def delete_ask_embedding_with_conn(conn, ask_id: int) -> None:
    """ask_vecから1行削除する（コミットは呼び出し側の責任）。

    vec0仮想テーブルは外部キー制約を持てないため、ask物理削除処理を実装する際は
    同じトランザクション内でこの関数を呼び、孤児レコードを残さないようにする。
    """
    conn.execute("DELETE FROM ask_vec WHERE rowid = ?", (ask_id,))
