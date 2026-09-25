"""pytest共通フィクスチャ。

ow workerセッション（OW_ROLE=worker）内でテストを実行すると、
hookやサービス層がworkerフロー扱いになり、通常セッション前提のテストが
非決定的に壊れる。テスト実行環境からow関連の環境変数を除去し、
どの環境で実行してもテストが決定論的に振る舞うようにする。
"""
import os
import shutil
import sqlite3
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
def _isolate_habits_rules_projection(tmp_path, monkeypatch):
    """habits投影ファイルの書き込み先をテストごとの一時パスへ差し替える。

    add_habit / update_habit / add_decisions はDBコミット成功後に
    ~/.claude/rules 配下の投影ファイルへの書き出しを自動で試みる。これが無いと、
    これらを呼ぶ既存テストが軒並み実際の ~/.claude/rules/cc-memory-habits.md を
    上書きしてしまう。
    """
    import src.config as config

    monkeypatch.setattr(config, "HABITS_RULES_PATH", str(tmp_path / "cc-memory-habits.md"))


@pytest.fixture(autouse=True)
def _isolate_relay_state_dir(tmp_path, monkeypatch):
    """relay の状態ディレクトリをテストごとの一時パスへ強制する。

    declaration / inbox / sessions への書込が本番 ~/.cc-memory/relay に
    到達すると、稼働中の全セッションの relay 受信を巻き込む。ファイル単位の
    opt-in fixture は書き忘れると本番を汚しうるため、全テストで無条件に
    分離する。
    """
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))


@pytest.fixture(autouse=True)
def _isolate_session_registry_files(tmp_path, monkeypatch):
    """セッション別名レジストリ・CLIセッションファイルの参照先をテストごとに隔離する。

    session_start_hookの打刻生存確認（session_registry_service.is_session_alive）が
    無隔離だと実行環境の~/.cc-memory/session_aliases.jsonや~/.claude/sessionsを
    読みにいき、テストがホストマシンの実セッション状態に依存し非決定的になる。
    """
    from src.services.session_registry_service import REGISTRY_PATH_ENV
    from src.infra.cli_session import CLAUDE_SESSIONS_DIR_ENV

    monkeypatch.setenv(REGISTRY_PATH_ENV, str(tmp_path / "session_aliases.json"))
    monkeypatch.setenv(CLAUDE_SESSIONS_DIR_ENV, str(tmp_path / "claude-sessions"))


@pytest.fixture(autouse=True)
def _isolate_claude_config(tmp_path, monkeypatch):
    """記録役の起動が信頼確認を登録する~/.claude.jsonをテストごとの一時パスへ向ける。

    recorder_launcher_service.startを通るテスト（autostart hook・見張りの
    restart経路を含む）が、実行環境の~/.claude.jsonにtmpパスのエントリを
    書き足してしまうのを防ぐ。
    """
    from src.services import recorder_launcher_service

    monkeypatch.setattr(
        recorder_launcher_service, "CLAUDE_CONFIG_PATH", tmp_path / "claude.json"
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

    precedent_telemetry（pull_precedents）も同じ daemon thread 非同期書込パターンを
    踏襲しているため、同じ race を避けるために同様に同期化する。
    """
    from src.services import precedent_pull_service, search_service

    def _make_synchronous_wrapper(original):
        def synchronous_wrapper(*args, **kwargs):
            thread = original(*args, **kwargs)
            if thread is not None:
                thread.join(timeout=5.0)
            return thread

        return synchronous_wrapper

    monkeypatch.setattr(
        search_service, "_record_search_telemetry_async",
        _make_synchronous_wrapper(search_service._record_search_telemetry_async),
    )
    monkeypatch.setattr(
        precedent_pull_service, "_record_precedent_telemetry_async",
        _make_synchronous_wrapper(precedent_pull_service._record_precedent_telemetry_async),
    )


@pytest.fixture(autouse=True)
def _synchronous_fetch_telemetry(monkeypatch):
    """fetch_telemetry (get_by_ids 計装) 書込を同期実行に切り替える。

    `_synchronous_telemetry` と同じ理由（daemon thread と TemporaryDirectory cleanup
    のレース回避）で、get_by_ids を呼ぶ既存テスト全般に波及するため autouse にする。
    """
    from src.services import search_service

    original = search_service._record_fetch_telemetry_async

    def synchronous_wrapper(*args, **kwargs):
        thread = original(*args, **kwargs)
        if thread is not None:
            thread.join(timeout=5.0)
        return thread

    monkeypatch.setattr(search_service, "_record_fetch_telemetry_async", synchronous_wrapper)


@pytest.fixture(autouse=True)
def _no_implicit_embedding_backfill(monkeypatch):
    """embeddingサーバー接続成立時に自動起動するバックフィルスレッドをテストでは起動させない。

    このスレッドはプロセスで1回だけ起動されてjoinされず、DBパスを接続のたびに
    環境変数から解決する。起動したテストの終了後も生き残り、temp_dbが切り替えた
    次のテストのDBへ書き込むため、そのテストの書き込みが database is locked で
    失敗する。バックフィル自体を検証するテストは _backfill_done=False を
    monkeypatchで明示して起動させる。
    """
    import src.services.embedding_service as emb
    monkeypatch.setattr(emb, "_backfill_done", True)


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


@pytest.fixture(scope="session")
def _temp_db_template(tmp_path_factory):
    """migration適用済みDBをテストセッション内で1回だけ構築するテンプレート。

    xdist使用時はワーカープロセスごとに1回構築される。init_database()は
    WALモードで接続するため、構築後に明示チェックポイントでWALをメイン
    ファイルへマージしてから返す(コピー先で-wal/-shmを気にしなくてよくする)。
    """
    from src.db import init_database

    template_dir = tmp_path_factory.mktemp("temp_db_template")
    db_path = str(template_dir / "template.db")
    prev_path = os.environ.get("DISCUSSION_DB_PATH")
    os.environ["DISCUSSION_DB_PATH"] = db_path
    try:
        init_database()
    finally:
        if prev_path is None:
            os.environ.pop("DISCUSSION_DB_PATH", None)
        else:
            os.environ["DISCUSSION_DB_PATH"] = prev_path

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return db_path


@pytest.fixture
def temp_db(_temp_db_template):
    """テスト用の一時SQLite DBを作成する共通フィクスチャ。

    migration適用済みのテンプレートDB(session scope、_temp_db_template)を
    コピーして構築コストを避ける。DISCUSSION_DB_PATH 環境変数を一時パスに
    切り替える。テスト終了時にtmpdirごと破棄される。
    """
    from src.services.checkin_tier_service import _greeted_sessions
    from src.services.tag_service import _injected_tags
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        shutil.copyfile(_temp_db_template, db_path)
        for suffix in ("-wal", "-shm"):
            aux_src = _temp_db_template + suffix
            if os.path.exists(aux_src):
                shutil.copyfile(aux_src, db_path + suffix)
        os.environ["DISCUSSION_DB_PATH"] = db_path
        _injected_tags.clear()
        _greeted_sessions.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]
