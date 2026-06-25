"""pytest共通フィクスチャ。

ow workerセッション（OW_ROLE=worker）内でテストを実行すると、
hookやサービス層がworkerフロー扱いになり、通常セッション前提のテストが
非決定的に壊れる。テスト実行環境からow関連の環境変数を除去し、
どの環境で実行してもテストが決定論的に振る舞うようにする。
"""
import os
import tempfile

import pytest

_OW_ENV_KEYS = (
    "OW_ROLE",
    "OW_ALIAS",
    "OW_CHANNEL",
    "OW_TASK_N",
    "OW_ESCALATION",
)


@pytest.fixture(autouse=True)
def _clear_ow_env(monkeypatch):
    """ow関連の環境変数をテストごとに除去する。

    OW_ROLE=worker等が実行環境にリークしていても、テストは通常セッション
    として振る舞う。worker挙動を検証するテストは個別にenvを設定すること。
    """
    for key in _OW_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _skip_sentinel_autospawn(monkeypatch):
    """ow_status 呼び出し時の sentinel.py auto-spawn をテストでは抑止する。

    ensure_sentinel_process (D#2752 Phase A 配線) はテスト中に呼ばれると
    永続 polling プロセスを残し、test runner 終了後もゾンビになる。
    OW_SKIP_SENTINEL_AUTOSPAWN=1 で default skip。auto-spawn 自体を検証する
    テストは個別に monkeypatch.delenv で外す。
    """
    monkeypatch.setenv("OW_SKIP_SENTINEL_AUTOSPAWN", "1")


@pytest.fixture(autouse=True)
def _synchronous_telemetry(monkeypatch):
    """search_telemetry 書込を同期実行に切り替える。

    本番は daemon thread で非同期書込するが、テストで daemon thread が
    生きているうちに TemporaryDirectory cleanup が走ると DB ファイルへの
    書込と rmtree が race して `OSError: Directory not empty` が出る。
    テスト中は書込スレッドを join してから search() を返すラッパに置き換え、
    レース無しで cleanup できるようにする。書込挙動自体 (Thread 生成 / daemon=True)
    は本番と同じ実装を通る (ラッパ内で original を呼ぶ) ため、
    本番の非同期性を検証するテストは join 後でも is_alive=False を assert できる。

    注意: 個別テストファイルの `capture_telemetry_threads` フィクスチャは、
    pytest のフィクスチャ適用順序上この `synchronous_wrapper` を更にラップする
    形になる。すなわち capture 側に渡ってくる thread は既にここで join() 済みで
    あり、`_wait_for_telemetry()` 側の join は実質 no-op になる。
    現状のテスト同期化はこの fixture (autouse) に依存しており、将来 autouse を
    解除する場合は capture 側でも join() を保証する必要がある。
    """
    from src.services import search_service

    original = search_service._record_search_telemetry_async

    def synchronous_wrapper(*args, **kwargs):
        thread = original(*args, **kwargs)
        if thread is not None:
            thread.join(timeout=5.0)
        return thread

    monkeypatch.setattr(search_service, "_record_search_telemetry_async", synchronous_wrapper)


@pytest.fixture
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化する共通フィクスチャ。

    DBクエリだけで完結するロジックの検証で、embeddingサーバー未起動状態を
    決定論的に再現するために使う。ファイル側で `autouse` ラップしたい場合は
    各テストファイルでこのfixtureに依存する薄いautouse fixtureを定義する。
    """
    import src.services.embedding_service as emb
    monkeypatch.setattr(emb, "_server_initialized", False)
    monkeypatch.setattr(emb, "_backfill_done", True)
    monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)


@pytest.fixture
def temp_db():
    """テスト用の一時SQLite DBを作成する共通フィクスチャ。

    DISCUSSION_DB_PATH 環境変数を一時パスに切り替え、init_database で
    スキーマを構築する。テスト終了時にtmpdirごと破棄される。
    """
    from src.db import init_database
    from src.services.tag_service import _injected_tags
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]
