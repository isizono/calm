"""backup_service のユニットテスト

kind別ディレクトリ・daily昇格ローテーション・整合性検証・防護つき復元
（サーバー稼働中チェック・互換性警告・prerestore退避・--file-copy）を検証する。

restore_snapshot() の稼働中チェックは実機のポート52837 lock file /
HTTPヘルスエンドポイントに依存するため、autouseフィクスチャで隔離する
（開発機上で実サーバーが稼働していてもテストは決定論的に振る舞う）。
"""
import argparse
import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.infra import lock_file
from src.services import backup_service as bs

# isolate_server_running_check(autouse)が_check_health_endpoint自体を差し替えるため、
# 本物の実装を検証するテスト(test_check_health_endpoint_bypasses_http_proxy_env)は
# ここで捕捉した実体に一時的に戻す。
_REAL_CHECK_HEALTH_ENDPOINT = bs._check_health_endpoint


@pytest.fixture(autouse=True)
def isolate_server_running_check(tmp_path, monkeypatch):
    """restore()の稼働中チェックを実機の状態から隔離する。"""
    lock_dir = tmp_path / ".cc-memory-lock-isolated"
    lock_dir.mkdir()
    monkeypatch.setattr(lock_file, "LOCK_DIR", lock_dir)
    monkeypatch.setattr(lock_file, "LOCK_FILE", lock_dir / "server.lock")
    monkeypatch.setattr(bs, "_check_health_endpoint", lambda timeout=2.0: False)


class _HealthOkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # テスト出力を汚さない


def test_check_health_endpoint_bypasses_http_proxy_env(monkeypatch):
    """_check_health_endpoint: HTTP_PROXY/http_proxyが設定されていても
    127.0.0.1へのヘルスチェックはプロキシを経由しない。

    実HTTPサーバーと実プロキシ環境変数を使い、本物のプロキシバイパスを
    確認する(NO_PROXY_OPENER自体の構成の退行はtest_embedding_service.pyの
    test_is_server_running_bypasses_http_proxy_envが担当する)。
    """
    import urllib.request

    from src import http_config

    monkeypatch.setattr(bs, "_check_health_endpoint", _REAL_CHECK_HEALTH_ENDPOINT)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthOkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        monkeypatch.setattr(http_config, "HTTP_HOST", "127.0.0.1")
        monkeypatch.setattr(http_config, "HTTP_PORT", port)
        # 閉じたポートを指すプロキシ。バイパスできていなければ接続拒否でFalseになる。
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        # urllib.request.urlopenの既定openerはプロセス内で初回呼び出し時に1度だけ
        # 構築されキャッシュされる。_check_health_endpointがNO_PROXY_OPENER経由では
        # なく素のurlopenへ戻る退行が起きても、既定openerが別テストで既にプロキシ
        # 無し状態のまま構築済みだと見逃しうるため、ここで作り直させる。
        monkeypatch.setattr(urllib.request, "_opener", None)
        assert bs._check_health_endpoint() is True
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _seed_activities(db_path: str, count: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for i in range(count):
            conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                (f"activity_{i}", "desc", "pending"),
            )
        conn.commit()
    finally:
        conn.close()


def _delete_activities(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM activities")
        conn.commit()
    finally:
        conn.close()


def _touch_snapshot(dir_path: Path, stem: str, created_at: str, kind: str = "periodic") -> tuple[Path, Path]:
    """ローテーション・一覧ロジックのテスト用に、内容を問わないダミーのスナップショットペアを置く。"""
    dir_path.mkdir(parents=True, exist_ok=True)
    db_path = dir_path / f"{stem}.db"
    json_path = dir_path / f"{stem}.json"
    db_path.write_bytes(b"")
    json_path.write_text(
        json.dumps({
            "created_at": created_at,
            "db_size_bytes": 0,
            "row_counts": {},
            "kind": kind,
            "schema_head": None,
            "quick_check": "ok",
        }),
        encoding="utf-8",
    )
    return db_path, json_path


class TestSnapshotDirFor:
    def test_periodic_is_top_level(self, temp_db):
        base = Path(temp_db).parent / "snapshots"
        assert bs.snapshot_dir_for(temp_db, "periodic") == base

    def test_other_kinds_are_subdirectories(self, temp_db):
        base = Path(temp_db).parent / "snapshots"
        for kind in ("premigration", "prerestore", "manual", "daily"):
            assert bs.snapshot_dir_for(temp_db, kind) == base / kind


class TestTakeSnapshotMetadata:
    def test_metadata_has_kind_schema_head_quick_check(self, temp_db):
        path = bs.take_snapshot(temp_db, kind="manual")
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))

        assert meta["kind"] == "manual"
        assert meta["schema_head"]  # init_databaseでyoyo適用済みなのでNoneではない
        assert meta["quick_check"] == "ok"
        assert "row_counts" in meta
        assert "discussion_topics" in meta["row_counts"]

    def test_db_and_json_always_paired(self, temp_db):
        path = bs.take_snapshot(temp_db, kind="manual")
        assert path.exists()
        assert path.with_suffix(".json").exists()

    def test_extra_metadata_is_merged(self, temp_db):
        path = bs.take_snapshot(
            temp_db, kind="premigration", extra_metadata={"pending_migrations": ["0049_x"]}
        )
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        assert meta["pending_migrations"] == ["0049_x"]
        assert meta["kind"] == "premigration"


class TestKindIndependentRotation:
    def test_periodic_rotation_does_not_touch_other_kinds(self, temp_db):
        premigration_dir = bs.snapshot_dir_for(temp_db, "premigration")
        bs.take_snapshot(temp_db, kind="premigration", max_snapshots=5)
        assert len(list(premigration_dir.glob("discussion_*.db"))) == 1

        periodic_dir = bs.snapshot_dir_for(temp_db, "periodic")
        _touch_snapshot(periodic_dir, "discussion_20260101_0000", "2026-01-01T00:00:00+00:00")
        _touch_snapshot(periodic_dir, "discussion_20260102_0000", "2026-01-02T00:00:00+00:00")
        bs.take_snapshot(temp_db, kind="periodic", max_snapshots=2)

        # premigrationは無傷のまま
        assert len(list(premigration_dir.glob("discussion_*.db"))) == 1


class TestDailyPromotion:
    """periodicのローテーション時のdaily/への昇格ロジック（§3.1.3）"""

    def test_promotes_when_no_daily_exists_for_date(self, temp_db):
        periodic_dir = bs.snapshot_dir_for(temp_db, "periodic")
        daily_dir = bs.snapshot_dir_for(temp_db, "daily")
        _touch_snapshot(periodic_dir, "discussion_20260101_0000", "2026-01-01T00:00:00+00:00")
        _touch_snapshot(periodic_dir, "discussion_20260102_0000", "2026-01-02T00:00:00+00:00")
        _touch_snapshot(periodic_dir, "discussion_20260103_0000", "2026-01-03T00:00:00+00:00")

        bs._rotate_periodic_with_promotion(temp_db, periodic_dir, max_snapshots=2)

        # 最古(0101)は削除ではなくdaily/へ移動している
        assert not (periodic_dir / "discussion_20260101_0000.db").exists()
        assert (daily_dir / "discussion_20260101_0000.db").exists()
        assert (daily_dir / "discussion_20260101_0000.json").exists()

        remaining = sorted(p.name for p in periodic_dir.glob("discussion_*.db"))
        assert remaining == ["discussion_20260102_0000.db", "discussion_20260103_0000.db"]

    def test_deletes_when_daily_already_has_date(self, temp_db):
        periodic_dir = bs.snapshot_dir_for(temp_db, "periodic")
        daily_dir = bs.snapshot_dir_for(temp_db, "daily")
        _touch_snapshot(daily_dir, "discussion_20260101_1200", "2026-01-01T12:00:00+00:00", kind="daily")
        _touch_snapshot(periodic_dir, "discussion_20260101_0000", "2026-01-01T00:00:00+00:00")
        _touch_snapshot(periodic_dir, "discussion_20260102_0000", "2026-01-02T00:00:00+00:00")
        _touch_snapshot(periodic_dir, "discussion_20260103_0000", "2026-01-03T00:00:00+00:00")

        bs._rotate_periodic_with_promotion(temp_db, periodic_dir, max_snapshots=2)

        assert not (periodic_dir / "discussion_20260101_0000.db").exists()
        assert not (daily_dir / "discussion_20260101_0000.db").exists()
        assert len(list(daily_dir.glob("discussion_*.db"))) == 1  # 元からの1件のみ

    def test_daily_does_not_exceed_own_quota(self, temp_db):
        periodic_dir = bs.snapshot_dir_for(temp_db, "periodic")
        daily_dir = bs.snapshot_dir_for(temp_db, "daily")
        for i in range(bs.KIND_QUOTAS["daily"]):
            _touch_snapshot(
                daily_dir, f"discussion_202601{i:02d}_0000", f"2026-01-{i + 1:02d}T00:00:00+00:00", kind="daily"
            )
        assert len(list(daily_dir.glob("discussion_*.db"))) == bs.KIND_QUOTAS["daily"]

        _touch_snapshot(periodic_dir, "discussion_20260201_0000", "2026-02-01T00:00:00+00:00")
        _touch_snapshot(periodic_dir, "discussion_20260202_0000", "2026-02-02T00:00:00+00:00")
        _touch_snapshot(periodic_dir, "discussion_20260203_0000", "2026-02-03T00:00:00+00:00")

        bs._rotate_periodic_with_promotion(temp_db, periodic_dir, max_snapshots=2)

        assert len(list(daily_dir.glob("discussion_*.db"))) == bs.KIND_QUOTAS["daily"]


class TestListSnapshots:
    def test_lists_across_all_kinds(self, temp_db):
        bs.take_snapshot(temp_db, kind="manual")
        bs.take_snapshot(temp_db, kind="premigration")

        results = bs.list_snapshots(temp_db)
        kinds = {r["kind"] for r in results}
        assert {"manual", "premigration"} <= kinds

    def test_sorted_by_created_at_desc(self, temp_db):
        periodic_dir = bs.snapshot_dir_for(temp_db, "periodic")
        _touch_snapshot(periodic_dir, "discussion_20260101_0000", "2026-01-01T00:00:00+00:00")
        _touch_snapshot(periodic_dir, "discussion_20260103_0000", "2026-01-03T00:00:00+00:00")
        _touch_snapshot(periodic_dir, "discussion_20260102_0000", "2026-01-02T00:00:00+00:00")

        created_ats = [r["created_at"] for r in bs.list_snapshots(temp_db)]
        assert created_ats == sorted(created_ats, reverse=True)


class TestVerifySnapshot:
    def test_valid_snapshot_is_ok(self, temp_db):
        path = bs.take_snapshot(temp_db, kind="manual")
        result = bs.verify_snapshot(str(path))

        assert result["ok"] is True
        assert result["integrity_check"] == "ok"
        assert "row_counts" in result

    def test_missing_file_is_not_ok(self, tmp_path):
        result = bs.verify_snapshot(str(tmp_path / "nope.db"))
        assert result["ok"] is False

    def test_truncated_file_is_detected_as_corrupt(self, temp_db, tmp_path):
        path = bs.take_snapshot(temp_db, kind="manual")
        original = path.read_bytes()
        truncated = tmp_path / "truncated.db"
        truncated.write_bytes(original[: max(1, len(original) // 4)])

        result = bs.verify_snapshot(str(truncated))
        assert result["ok"] is False

    def test_garbage_file_is_detected_as_corrupt(self, tmp_path):
        garbage = tmp_path / "garbage.db"
        garbage.write_bytes(b"not a sqlite database" * 100)

        result = bs.verify_snapshot(str(garbage))
        assert result["ok"] is False


class TestSchemaCompatibility:
    def test_none_schema_head_gives_note_only(self):
        result = bs._check_schema_compatibility(None)
        assert result.warning is None
        assert result.note is not None

    def test_current_head_gives_no_message(self):
        current = bs._current_migration_ids()
        result = bs._check_schema_compatibility(current[-1])
        assert result.warning is None
        assert result.note is None

    def test_older_head_gives_note_not_warning(self):
        current = bs._current_migration_ids()
        assert len(current) >= 2
        result = bs._check_schema_compatibility(current[0])
        assert result.warning is None
        assert result.note is not None

    def test_unknown_head_gives_blocking_warning(self):
        result = bs._check_schema_compatibility("9999_does_not_exist")
        assert result.warning is not None


class TestRestoreRoundTrip:
    def test_restore_recovers_deleted_rows_and_round_trips(self, temp_db):
        _seed_activities(temp_db, 5)
        snapshot_path = bs.take_snapshot(temp_db, kind="manual")
        before_counts = bs.get_row_counts(temp_db)

        _delete_activities(temp_db)
        assert bs.get_row_counts(temp_db)["activities"] == 0

        result = bs.restore_snapshot(str(snapshot_path), temp_db)

        assert bs.get_row_counts(temp_db)["activities"] == before_counts["activities"]
        assert result.prerestore_path is not None
        assert Path(result.prerestore_path).exists()

        # prerestoreは「削除直後(0件)」の状態を退避しているので、そこへ戻すと0件に戻る
        bs.restore_snapshot(result.prerestore_path, temp_db)
        assert bs.get_row_counts(temp_db)["activities"] == 0

    def test_repeated_restore_from_prerestore_survives_quota_rotation(self, temp_db):
        """prerestore退避のquota超過ローテーションが復元元を消してDBを空にしないこと。

        prerestore quota=2。同一prerestoreスナップショットからの復元を3回続けると、
        3回目の退避で古いprerestoreが1件ローテーション削除される。復元元がその最古と
        一致する場合、削除直後にそのファイルから復元しようとして空DBで上書きされうる。
        復元元をローテーションから保護することで、復元後も行数が保たれることを検証する。
        """
        _seed_activities(temp_db, 5)
        manual_snapshot = bs.take_snapshot(temp_db, kind="manual")

        # 1回目: prerestore P1（5件の状態）を生成
        result1 = bs.restore_snapshot(str(manual_snapshot), temp_db)
        p1 = result1.prerestore_path
        assert p1 is not None

        # 2回目: P1から復元 → prerestore P2 生成（prerestoreは2件、quota内）
        bs.restore_snapshot(p1, temp_db)
        assert bs.get_row_counts(temp_db)["activities"] == 5

        # 3回目: P1から復元 → prerestore P3 生成で最古(=P1)がローテーション対象になる。
        # 保護が無いとP1が消え、空DBで現行DBが上書きされ0件になる。
        bs.restore_snapshot(p1, temp_db)
        assert Path(p1).exists()  # 復元元は保護され生存している
        assert bs.get_row_counts(temp_db)["activities"] == 5  # 無警告のデータ消失が起きていない


class TestRestoreServerRunningGuard:
    def test_blocked_when_lock_file_shows_alive_process(self, temp_db):
        snapshot_path = bs.take_snapshot(temp_db, kind="manual")
        lock_file.acquire(52837)  # 自プロセスPIDなのでis_process_alive=True

        with pytest.raises(bs.RestoreBlockedError):
            bs.restore_snapshot(str(snapshot_path), temp_db)

    def test_blocked_message_points_to_restart_server_cli_not_lsof(self, temp_db):
        """案内文はWindowsに存在しないlsofではなく、OS非依存のCLIを示す。

        `${CLAUDE_PLUGIN_ROOT}`のようなシェル変数テンプレートを文字列のまま
        出力する実装に戻しても、"restart_server.py"・"--stop"の存在と
        "lsof"の不在だけでは検知できないため、テンプレート変数が残っていない
        ことと、案内文中のパスが実在するファイルを指すことまで確かめる。
        """
        snapshot_path = bs.take_snapshot(temp_db, kind="manual")
        lock_file.acquire(52837)

        with pytest.raises(bs.RestoreBlockedError) as exc_info:
            bs.restore_snapshot(str(snapshot_path), temp_db)

        message = str(exc_info.value)
        assert "restart_server.py" in message
        assert "--stop" in message
        assert "lsof" not in message
        assert "${" not in message

        match = re.search(r'python "([^"]+restart_server\.py)"', message)
        assert match is not None, message
        assert Path(match.group(1)).exists()

    def test_force_bypasses_running_check(self, temp_db):
        snapshot_path = bs.take_snapshot(temp_db, kind="manual")
        lock_file.acquire(52837)

        result = bs.restore_snapshot(str(snapshot_path), temp_db, force=True)
        assert result.forced is True

    def test_blocked_when_health_endpoint_responds(self, temp_db, monkeypatch):
        snapshot_path = bs.take_snapshot(temp_db, kind="manual")
        monkeypatch.setattr(bs, "_check_health_endpoint", lambda timeout=2.0: True)

        with pytest.raises(bs.RestoreBlockedError):
            bs.restore_snapshot(str(snapshot_path), temp_db)


class TestRestoreVerificationGuard:
    def test_blocked_on_corrupt_snapshot(self, temp_db, tmp_path):
        garbage = tmp_path / "discussion_20260101_0000.db"
        garbage.write_bytes(b"not a sqlite database" * 50)
        garbage.with_suffix(".json").write_text(json.dumps({"schema_head": None}), encoding="utf-8")

        with pytest.raises(bs.RestoreBlockedError):
            bs.restore_snapshot(str(garbage), temp_db)

    def test_missing_snapshot_file_raises(self, temp_db, tmp_path):
        with pytest.raises(FileNotFoundError):
            bs.restore_snapshot(str(tmp_path / "nope.db"), temp_db)


class TestRestoreCompatibilityGuard:
    def test_blocked_without_yes_for_unknown_schema_head(self, temp_db):
        snapshot_path = bs.take_snapshot(temp_db, kind="manual")
        meta_path = snapshot_path.with_suffix(".json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["schema_head"] = "9999_does_not_exist"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        with pytest.raises(bs.RestoreBlockedError):
            bs.restore_snapshot(str(snapshot_path), temp_db)

    def test_yes_bypasses_compatibility_guard(self, temp_db):
        snapshot_path = bs.take_snapshot(temp_db, kind="manual")
        meta_path = snapshot_path.with_suffix(".json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["schema_head"] = "9999_does_not_exist"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        result = bs.restore_snapshot(str(snapshot_path), temp_db, yes=True)
        assert result.restored_from == str(snapshot_path)


class TestRestoreFileCopy:
    def test_file_copy_replaces_db_and_removes_wal_shm(self, temp_db):
        snapshot_path = bs.take_snapshot(temp_db, kind="manual")

        wal = Path(f"{temp_db}-wal")
        shm = Path(f"{temp_db}-shm")
        wal.write_bytes(b"dummy-wal")
        shm.write_bytes(b"dummy-shm")

        bs.restore_snapshot(str(snapshot_path), temp_db, file_copy=True)

        assert not wal.exists()
        assert not shm.exists()
        assert bs.get_row_counts(temp_db) is not None

    def test_file_copy_aborts_without_touching_db_when_sidecar_delete_fails(
        self, temp_db, monkeypatch
    ):
        """-walの削除が失敗(Windowsの共有違反相当)した場合、本体は上書きされず
        RestoreBlockedErrorで中断する（DBを開いたプロセスが残っている状態での
        復元が、本体とWALが食い違う不整合な状態を作らないようにするため）。"""
        snapshot_path = bs.take_snapshot(temp_db, kind="manual")

        wal = Path(f"{temp_db}-wal")
        wal.write_bytes(b"dummy-wal")
        original_db_bytes = Path(temp_db).read_bytes()

        real_unlink = Path.unlink

        def flaky_unlink(self, missing_ok=False):
            if self == wal:
                raise OSError(32, "The process cannot access the file")
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", flaky_unlink)

        with pytest.raises(bs.RestoreBlockedError):
            bs.restore_snapshot(str(snapshot_path), temp_db, file_copy=True)

        # 本体はコピー前に中断しているため元のバイト列のまま
        assert Path(temp_db).read_bytes() == original_db_bytes


class TestRestorePrerestoreFallback:
    def test_falls_back_to_raw_file_copy_when_current_db_unreadable(self, temp_db):
        snapshot_path = bs.take_snapshot(temp_db, kind="manual")

        # backup APIで読めない状態まで現行DBを破損させる
        Path(temp_db).write_bytes(b"corrupted-not-a-db" * 20)

        result = bs.restore_snapshot(str(snapshot_path), temp_db, file_copy=True)

        assert result.prerestore_path is not None
        prerestore_bytes = Path(result.prerestore_path).read_bytes()
        assert prerestore_bytes.startswith(b"corrupted-not-a-db")
        # 復元後は正常なDBに戻っている
        assert isinstance(bs.get_row_counts(temp_db), dict)

    def test_fallback_copy_writes_json_and_is_listed(self, temp_db):
        """破損DBフォールバック退避にもJSONが添えられ、list_snapshotsから可視になること。

        JSONが無いとlist_snapshots()（.json起点で列挙）から漏れ、破損時の唯一の退避先が
        ユーザーに見えなくなる。フォールバックコピーがメタデータJSON付きで作られ、
        一覧にfallbackフラグ付きで現れることを検証する。
        """
        snapshot_path = bs.take_snapshot(temp_db, kind="manual")

        Path(temp_db).write_bytes(b"corrupted-not-a-db" * 20)

        result = bs.restore_snapshot(str(snapshot_path), temp_db, file_copy=True)
        fallback_db = Path(result.prerestore_path)
        fallback_json = fallback_db.with_suffix(".json")

        assert fallback_json.exists()
        meta = json.loads(fallback_json.read_text(encoding="utf-8"))
        assert meta["kind"] == "prerestore"
        assert meta["fallback"] is True

        listed = bs.list_snapshots(temp_db)
        listed_db_paths = {entry["db_path"] for entry in listed}
        assert str(fallback_db) in listed_db_paths


class TestUniqueSnapshotPaths:
    def test_reserves_db_atomically_to_avoid_collision(self, tmp_path):
        """_unique_snapshot_pathsが.dbをその場で確保し、連続呼び出しで別名を返すこと。

        名前を計算するだけで確保しないと、同一秒内の複数プロセス/連続呼び出しが同じ名前を
        選び片方の書き込みが失われうる。呼び出し即座に.dbを排他作成することで衝突を防ぐ。
        """
        snapshot_dir = tmp_path / "snaps"

        first_db, first_json = bs._unique_snapshot_paths(snapshot_dir)
        assert first_db.exists()  # 呼び出し時点で.dbが確保済み
        assert first_db.suffix == ".db"
        assert first_json.suffix == ".json"

        second_db, _second_json = bs._unique_snapshot_paths(snapshot_dir)
        assert second_db.exists()
        assert second_db != first_db  # 確保済みの第1と衝突しない別名


class TestCLIHandlers:
    def test_cmd_list_reports_no_snapshots(self, temp_db, capsys):
        assert bs._cmd_list(argparse.Namespace(db_path=temp_db)) == 0
        assert "スナップショットはありません" in capsys.readouterr().out

    def test_cmd_take_then_list(self, temp_db, capsys):
        assert bs._cmd_take(argparse.Namespace(db_path=temp_db, kind="manual")) == 0
        capsys.readouterr()

        assert bs._cmd_list(argparse.Namespace(db_path=temp_db)) == 0
        assert "manual" in capsys.readouterr().out

    def test_cmd_verify_ok_and_ng_exit_codes(self, temp_db, tmp_path):
        good_path = bs.take_snapshot(temp_db, kind="manual")
        assert bs._cmd_verify(argparse.Namespace(path=str(good_path))) == 0

        garbage = tmp_path / "garbage.db"
        garbage.write_bytes(b"nope" * 50)
        assert bs._cmd_verify(argparse.Namespace(path=str(garbage))) == 1

    def test_cmd_restore_latest_picks_newest_across_kinds(self, temp_db):
        _seed_activities(temp_db, 3)
        old_snapshot = bs.take_snapshot(temp_db, kind="manual")
        old_meta_path = old_snapshot.with_suffix(".json")
        old_meta = json.loads(old_meta_path.read_text(encoding="utf-8"))
        old_meta["created_at"] = "2020-01-01T00:00:00+00:00"
        old_meta_path.write_text(json.dumps(old_meta), encoding="utf-8")

        _seed_activities(temp_db, 7)  # 合計10件
        new_snapshot = bs.take_snapshot(temp_db, kind="premigration")
        new_meta_path = new_snapshot.with_suffix(".json")
        new_meta = json.loads(new_meta_path.read_text(encoding="utf-8"))
        new_meta["created_at"] = "2030-01-01T00:00:00+00:00"
        new_meta_path.write_text(json.dumps(new_meta), encoding="utf-8")

        _delete_activities(temp_db)

        args = argparse.Namespace(
            db_path=temp_db, latest=True, path=None, force=False, file_copy=False, yes=False,
        )
        assert bs._cmd_restore(args) == 0
        assert bs.get_row_counts(temp_db)["activities"] == 10
