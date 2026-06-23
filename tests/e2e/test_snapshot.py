"""scripts/snapshot.py の E2E テスト

スナップショット取得・ヘルスチェック・ローテーション・復元、
および session_start_hook.py との統合テスト。
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.db import init_database
from scripts.snapshot import (
    get_row_counts,
    health_check,
    should_take_snapshot,
    take_snapshot,
    restore_snapshot,
    HealthCheckResult,
    SNAPSHOT_PREFIX,
    SNAPSHOT_JSON_SUFFIX,
)

# プロジェクトルート
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    import src.config
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        src.config.DB_PATH = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]
        src.config.DB_PATH = None


def _seed_rows(db_path: str, table: str, count: int) -> None:
    """指定テーブルにダミー行を追加する"""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        if table == "discussion_topics":
            for i in range(count):
                conn.execute(
                    "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                    (f"topic_{i}", "desc"),
                )
        elif table == "decisions":
            # 親 topic との紐付けは relations.belongs_to で表現する
            topic_row = conn.execute("SELECT id FROM discussion_topics LIMIT 1").fetchone()
            topic_id = topic_row[0] if topic_row else 1
            for i in range(count):
                cur = conn.execute(
                    "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
                    (f"decision_{i}", "reason"),
                )
                conn.execute(
                    "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) "
                    "VALUES ('decision', ?, 'topic', ?, 'belongs_to')",
                    (cur.lastrowid, topic_id),
                )
        elif table == "discussion_logs":
            topic_row = conn.execute("SELECT id FROM discussion_topics LIMIT 1").fetchone()
            topic_id = topic_row[0] if topic_row else 1
            for i in range(count):
                cur = conn.execute(
                    "INSERT INTO discussion_logs (content) VALUES (?)",
                    (f"log_{i}",),
                )
                conn.execute(
                    "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) "
                    "VALUES ('log', ?, 'topic', ?, 'belongs_to')",
                    (cur.lastrowid, topic_id),
                )
        elif table == "activities":
            for i in range(count):
                conn.execute(
                    "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                    (f"activity_{i}", "desc", "pending"),
                )
        elif table == "materials":
            for i in range(count):
                conn.execute(
                    "INSERT INTO materials (title, content) VALUES (?, ?)",
                    (f"material_{i}", "content"),
                )
        conn.commit()
    finally:
        conn.close()


class TestTakeSnapshot:
    """スナップショット取得のテスト"""

    def test_take_snapshot_creates_db_and_json(self, temp_db):
        """スナップショット取得でDBとJSONが作成される"""
        snapshot_dir = Path(temp_db).parent / "snapshots"

        result_path = take_snapshot(temp_db, snapshot_dir)

        assert result_path.exists()
        assert result_path.suffix == ".db"

        json_path = result_path.with_suffix(".json")
        assert json_path.exists()

        metadata = json.loads(json_path.read_text(encoding="utf-8"))
        assert "created_at" in metadata
        assert "db_size_bytes" in metadata
        assert "row_counts" in metadata
        assert isinstance(metadata["row_counts"], dict)
        assert "discussion_topics" in metadata["row_counts"]

    def test_snapshot_rotation(self, temp_db):
        """max_snapshots超過時に古いものが削除される"""
        snapshot_dir = Path(temp_db).parent / "snapshots"
        max_snapshots = 3

        created_paths = []
        for i in range(5):
            # タイムスタンプをずらすためにファイル名を直接操作するのではなく、
            # 毎回take_snapshotを呼ぶ（同一分内なら同名になるので手動で管理）
            path = take_snapshot(temp_db, snapshot_dir, max_snapshots=max_snapshots)
            created_paths.append(path)

            # 同一分内の重複を避けるため、作成済みのファイルを別名にリネーム
            if i < 4:
                new_stem = f"discussion_2026010{i}_000{i}"
                new_db = snapshot_dir / f"{new_stem}.db"
                new_json = snapshot_dir / f"{new_stem}.json"
                path.rename(new_db)
                json_path = path.with_suffix(".json")
                if json_path.exists():
                    # JSONのcreated_atも更新
                    meta = json.loads(json_path.read_text(encoding="utf-8"))
                    meta["created_at"] = f"2026-01-0{i+1}T00:0{i}:00+00:00"
                    new_json.write_text(json.dumps(meta), encoding="utf-8")
                    json_path.unlink()

        db_files = list(snapshot_dir.glob(f"{SNAPSHOT_PREFIX}*.db"))
        assert len(db_files) == max_snapshots


class TestHealthCheck:
    """ヘルスチェックのテスト"""

    def test_health_check_healthy(self, temp_db):
        """行数増加/維持でis_healthy=True"""
        snapshot_dir = Path(temp_db).parent / "snapshots"

        # 初回スナップショット取得
        take_snapshot(temp_db, snapshot_dir)

        # 行数を増加させる
        _seed_rows(temp_db, "activities", 10)

        result = health_check(temp_db, snapshot_dir)
        assert result.is_healthy is True
        assert len(result.warnings) == 0

    def test_health_check_anomaly(self, temp_db):
        """100件以上減少でis_healthy=False, warningsにテーブル名を含む"""
        snapshot_dir = Path(temp_db).parent / "snapshots"

        # 大量の行を追加してスナップショット取得
        _seed_rows(temp_db, "activities", 150)
        take_snapshot(temp_db, snapshot_dir)

        # 行を削除（100件以上の減少を発生させる）
        import sqlite3
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute("DELETE FROM activities")
            conn.commit()
        finally:
            conn.close()

        result = health_check(temp_db, snapshot_dir, threshold=100)
        assert result.is_healthy is False
        assert len(result.warnings) > 0
        # warningsにテーブル名が含まれる
        warning_text = "\n".join(result.warnings)
        assert "activities" in warning_text

    def test_health_check_no_snapshot(self, temp_db):
        """スナップショットなし（初回）でis_healthy=True"""
        snapshot_dir = Path(temp_db).parent / "snapshots"

        result = health_check(temp_db, snapshot_dir)
        assert result.is_healthy is True
        assert len(result.warnings) == 0


class TestShouldTakeSnapshot:
    """スナップショット間隔チェックのテスト"""

    def test_should_take_snapshot_no_existing(self, temp_db):
        """スナップショットなしの場合はTrue"""
        snapshot_dir = Path(temp_db).parent / "snapshots"
        assert should_take_snapshot(snapshot_dir, interval_hours=12) is True

    def test_should_take_snapshot_interval_not_elapsed(self, temp_db):
        """間隔未経過の場合はFalse"""
        snapshot_dir = Path(temp_db).parent / "snapshots"

        # スナップショット取得（現在時刻で作成される）
        take_snapshot(temp_db, snapshot_dir)

        # 12時間経過していないのでFalse
        assert should_take_snapshot(snapshot_dir, interval_hours=12) is False

    def test_should_take_snapshot_interval_elapsed(self, temp_db):
        """間隔経過後はTrue"""
        snapshot_dir = Path(temp_db).parent / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        # 古いスナップショットJSONを手動作成
        old_time = datetime.now(timezone.utc) - timedelta(hours=13)
        meta = {
            "created_at": old_time.isoformat(),
            "db_size_bytes": 1000,
            "row_counts": {},
        }
        json_path = snapshot_dir / f"{SNAPSHOT_PREFIX}20260101_0000{SNAPSHOT_JSON_SUFFIX}"
        json_path.write_text(json.dumps(meta), encoding="utf-8")

        assert should_take_snapshot(snapshot_dir, interval_hours=12) is True


class TestRestoreSnapshot:
    """復元のテスト"""

    def test_restore_snapshot(self, temp_db):
        """復元後にデータが戻る"""
        snapshot_dir = Path(temp_db).parent / "snapshots"

        # データを追加してスナップショット取得
        _seed_rows(temp_db, "activities", 50)
        snapshot_path = take_snapshot(temp_db, snapshot_dir)

        # 取得時の行数を記録
        original_counts = get_row_counts(temp_db)

        # データを削除
        import sqlite3
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute("DELETE FROM activities")
            conn.commit()
        finally:
            conn.close()

        # 削除後の行数確認
        deleted_counts = get_row_counts(temp_db)
        assert deleted_counts["activities"] == 0

        # 復元
        restore_snapshot(str(snapshot_path), temp_db)

        # 復元後の行数確認
        restored_counts = get_row_counts(temp_db)
        assert restored_counts["activities"] == original_counts["activities"]


def _run_session_start_hook(db_path: str) -> dict:
    """session_start_hook.pyを実行してJSON出力を返す"""
    env = {**os.environ, "DISCUSSION_DB_PATH": db_path}

    result = subprocess.run(
        [sys.executable, "hooks/session_start_hook.py"],
        input="{}",
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
    )

    stdout = result.stdout.strip()
    assert stdout, f"session_start_hook.py produced no output. stderr: {result.stderr}"
    return json.loads(stdout)


class TestSessionStartHookSnapshot:
    """session_start_hook.py とスナップショットの統合テスト"""

    def test_session_start_hook_with_snapshot(self, temp_db):
        """hookのE2E: 正常時は警告なし"""
        result = _run_session_start_hook(temp_db)

        context = result["hookSpecificOutput"]["additionalContext"]
        # 正常時は警告メッセージが含まれない
        assert "\U0001f6a8" not in context
        assert "DBデータ異常減少" not in context

    def test_session_start_hook_anomaly_warning(self, temp_db):
        """hookのE2E: 行削除→subprocess実行で警告メッセージが出る"""
        snapshot_dir = Path(temp_db).parent / "snapshots"

        # 大量の行を追加してスナップショット取得
        _seed_rows(temp_db, "activities", 200)
        take_snapshot(temp_db, snapshot_dir)

        # 行を削除して異常状態を作る
        import sqlite3
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute("DELETE FROM activities")
            conn.commit()
        finally:
            conn.close()

        result = _run_session_start_hook(temp_db)

        context = result["hookSpecificOutput"]["additionalContext"]
        # 警告メッセージが含まれる
        assert "DBデータ異常減少" in context
        assert "activities" in context
        assert "復元" in context

    def test_session_start_hook_anomaly_no_new_snapshot(self, temp_db):
        """hookのE2E: 異常検知時にスナップショットが新規作成されない"""
        snapshot_dir = Path(temp_db).parent / "snapshots"

        # 大量の行を追加してスナップショット取得
        _seed_rows(temp_db, "activities", 200)
        take_snapshot(temp_db, snapshot_dir)

        # 取得直後のスナップショット数を記録
        snapshots_before = list(snapshot_dir.glob(f"{SNAPSHOT_PREFIX}*.db"))

        # 行を削除して異常状態を作る
        import sqlite3
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute("DELETE FROM activities")
            conn.commit()
        finally:
            conn.close()

        # hookを実行（異常検知 → スナップショット取得しないはず）
        _run_session_start_hook(temp_db)

        # スナップショット数が増えていないこと
        snapshots_after = list(snapshot_dir.glob(f"{SNAPSHOT_PREFIX}*.db"))
        assert len(snapshots_after) == len(snapshots_before)
