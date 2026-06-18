"""pytest共通フィクスチャ。

ow workerセッション（OW_ROLE=worker）内でテストを実行すると、
hookやサービス層がworkerフロー扱いになり、通常セッション前提のテストが
非決定的に壊れる。テスト実行環境からow関連の環境変数を除去し、
どの環境で実行してもテストが決定論的に振る舞うようにする。
"""
import os
import tempfile

import pytest

_OW_ENV_KEYS = ("OW_ROLE", "OW_ALIAS", "OW_CHANNEL", "OW_TASK_FILE")


@pytest.fixture(autouse=True)
def _clear_ow_env(monkeypatch):
    """ow関連の環境変数をテストごとに除去する。

    OW_ROLE=worker等が実行環境にリークしていても、テストは通常セッション
    として振る舞う。worker挙動を検証するテストは個別にenvを設定すること。
    """
    for key in _OW_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


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
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]
