"""habit_projectionのユニットテスト

tests/conftest.py の autouse fixture (_isolate_habits_rules_projection) が
投影ファイルの書き込み先をテストごとの一時パスへ差し替えている。実際の
~/.claude/rules/cc-memory-habits.md には一切書き込まない。
"""
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import config
from src.db import get_connection
from src.services import habit_projection
from src.services.decision_service import add_decisions
from src.services.habit_service import add_habit, get_habits, update_habit
from src.services.topic_service import add_topic


@pytest.fixture
def projection_path():
    """autouse fixtureが差し替え済みの投影先パスをPathとして返す。"""
    return Path(config.HABITS_RULES_PATH)


def _clear_seed_habits(conn) -> None:
    """migration由来の初期habitsを無効化し、テストを0件から決定論的に始める。"""
    conn.execute("UPDATE habits SET active = 0")
    conn.commit()


def _add_always(content: str) -> int:
    """ゲートを経由せず、直接trigger_mode='always'の振る舞いを作る（render検証専用）。"""
    habit_id = add_habit(content)["habit_id"]
    conn = get_connection()
    try:
        conn.execute("UPDATE habits SET trigger_mode = 'always' WHERE id = ?", (habit_id,))
        conn.commit()
    finally:
        conn.close()
    return habit_id


class TestRenderBody:
    """render_bodyのテスト（決定論・構成）"""

    def test_always_full_text_and_intelligently_manifest(self, temp_db):
        conn = get_connection()
        try:
            _clear_seed_habits(conn)
        finally:
            conn.close()

        _add_always("常時注入される振る舞い")
        intelligently_id = add_habit("マニフェストに載る振る舞い")["habit_id"]

        conn = get_connection()
        try:
            body = habit_projection.render_body(conn)
        finally:
            conn.close()

        assert "常時注入される振る舞い" in body
        assert "マニフェストに載る振る舞い" in body
        assert f"habit_id={intelligently_id}" in body

    def test_inactive_habit_not_projected(self, temp_db):
        conn = get_connection()
        try:
            _clear_seed_habits(conn)
        finally:
            conn.close()

        habit_id = add_habit("無効化される振る舞い")["habit_id"]
        update_habit(habit_id, active=False)

        conn = get_connection()
        try:
            body = habit_projection.render_body(conn)
        finally:
            conn.close()

        assert "無効化される振る舞い" not in body

    def test_zero_habits_placeholder(self, temp_db):
        conn = get_connection()
        try:
            _clear_seed_habits(conn)
            body = habit_projection.render_body(conn)
        finally:
            conn.close()

        assert "（現在有効な habits はない）" in body

    def test_multiline_content_normalized_to_single_line(self, temp_db):
        """always層のcontentに改行が含まれても箇条書き1行に正規化されること。

        正規化前は「- 1行目\n2行目」のように2行目以降が箇条書きプレフィックス
        なしで出力され、投影ファイルのMarkdown構造が崩れていた。
        """
        conn = get_connection()
        try:
            _clear_seed_habits(conn)
        finally:
            conn.close()

        _add_always("1行目\n2行目\n3行目")

        conn = get_connection()
        try:
            body = habit_projection.render_body(conn)
        finally:
            conn.close()

        assert "- 1行目 2行目 3行目" in body
        assert "\n2行目" not in body
        assert "\n3行目" not in body

    def test_multiline_title_normalized_in_manifest(self, temp_db):
        """intelligently層マニフェストのtitle（content先頭50文字）に改行が
        含まれても箇条書き1行に正規化されること。
        """
        conn = get_connection()
        try:
            _clear_seed_habits(conn)
        finally:
            conn.close()

        habit_id = add_habit("マニフェスト\n改行タイトル用habit")["habit_id"]

        conn = get_connection()
        try:
            body = habit_projection.render_body(conn)
        finally:
            conn.close()

        assert f"- マニフェスト 改行タイトル用habit (habit_id={habit_id})" in body

    def test_deterministic_same_db_state(self, temp_db):
        conn = get_connection()
        try:
            _clear_seed_habits(conn)
        finally:
            conn.close()

        _add_always("再現性のある振る舞いA")
        add_habit("再現性のある振る舞いB")

        conn = get_connection()
        try:
            body1 = habit_projection.render_body(conn)
            body2 = habit_projection.render_body(conn)
        finally:
            conn.close()

        assert body1 == body2
        assert habit_projection.compute_hash(body1) == habit_projection.compute_hash(body2)


class TestManifestBudget:
    """intelligently層マニフェストの独立予算のテスト"""

    def test_within_budget_lists_all_items(self, temp_db, monkeypatch):
        monkeypatch.setattr(config, "PROJECTION_MANIFEST_MAX_ITEMS", 30)
        conn = get_connection()
        try:
            _clear_seed_habits(conn)
        finally:
            conn.close()

        habit_ids = [add_habit(f"マニフェスト項目{i}")["habit_id"] for i in range(30)]

        conn = get_connection()
        try:
            body = habit_projection.render_body(conn)
        finally:
            conn.close()

        for habit_id in habit_ids:
            assert f"habit_id={habit_id}" in body
        assert "→ get_habits で確認" not in body

    def test_over_budget_collapses_to_count_line(self, temp_db, monkeypatch):
        monkeypatch.setattr(config, "PROJECTION_MANIFEST_MAX_ITEMS", 30)
        conn = get_connection()
        try:
            _clear_seed_habits(conn)
        finally:
            conn.close()

        for i in range(31):
            add_habit(f"マニフェスト項目{i}")

        conn = get_connection()
        try:
            body = habit_projection.render_body(conn)
        finally:
            conn.close()

        assert "他1件" in body
        assert "get_habits" in body


class TestFileStateRoundtrip:
    """render_file / read_file_state のラウンドトリップと破損検知のテスト"""

    def test_roundtrip(self, tmp_path):
        body = "# test\n\n- a\n"
        now = datetime(2026, 7, 9, 10, 0, 0, tzinfo=timezone.utc)
        content = habit_projection.render_file(body, now)
        path = tmp_path / "out.md"
        path.write_text(content, encoding="utf-8")

        state = habit_projection.read_file_state(path)

        assert state.status == "ok"
        assert state.body == body
        assert state.meta_hash == habit_projection.compute_hash(body)
        assert state.body_hash == habit_projection.compute_hash(body)

    def test_absent_when_file_missing(self, tmp_path):
        state = habit_projection.read_file_state(tmp_path / "missing.md")

        assert state.status == "absent"

    def test_absent_when_metadata_comment_missing(self, tmp_path):
        path = tmp_path / "broken.md"
        path.write_text("# 振る舞い\n\n本文のみ、メタデータコメントなし\n", encoding="utf-8")

        state = habit_projection.read_file_state(path)

        assert state.status == "absent"


class TestExport:
    """exportのテスト"""

    def test_creates_missing_directory(self, temp_db, tmp_path, monkeypatch):
        target = tmp_path / "nested" / "dir" / "cc-memory-habits.md"
        monkeypatch.setattr(config, "HABITS_RULES_PATH", str(target))

        result = habit_projection.export()

        assert result["status"] == "written"
        assert target.exists()

    def test_unchanged_content_skips_write_and_preserves_mtime(self, temp_db, projection_path):
        habit_projection.export()
        mtime_before = projection_path.stat().st_mtime

        result = habit_projection.export()

        assert result["status"] == "skipped"
        assert projection_path.stat().st_mtime == mtime_before

    def test_no_leftover_tmp_file_after_write(self, temp_db, projection_path):
        habit_projection.export()

        assert list(projection_path.parent.glob(".cc-memory-habits-*.tmp")) == []

    def test_write_failure_returns_failed_status_without_raising(self, temp_db, tmp_path, monkeypatch):
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        target = readonly_dir / "cc-memory-habits.md"
        monkeypatch.setattr(config, "HABITS_RULES_PATH", str(target))
        readonly_dir.chmod(0o500)
        try:
            result = habit_projection.export()
        finally:
            readonly_dir.chmod(0o700)

        assert result["status"] == "failed"
        assert "message" in result

    def test_concurrent_export_from_multiple_threads_does_not_corrupt_file(
        self, temp_db, projection_path, monkeypatch
    ):
        """同一プロセス内の複数スレッドから export() を並行呼び出ししても、
        一時ファイル名の衝突による書き込み失敗・破損が起きないこと。

        本サーバーはFastMCP上でsyncなツール関数を提供しており、複数セッションから
        のツール呼び出しが同一プロセス内で並行実行されうる。修正前は一時ファイル名が
        os.getpid()のみで構成され、同一プロセス内の全スレッドで同じ値になるため、
        2スレッドがほぼ同時にexportすると一方の os.replace が既に相手に消費された
        一時ファイルを対象にして FileNotFoundError で失敗しうる。os.replace の直前に
        barrierで両スレッドの足並みを揃えることで、そのレースを決定論的に再現する。
        """
        add_habit("並行書き込みテスト用habit")

        barrier = threading.Barrier(2)
        original_replace = os.replace

        def synced_replace(src, dst):
            # 両スレッドがtmpファイルへのwrite_textを終えた後、os.replace直前で
            # 足並みを揃える。tmpファイル名が衝突していれば、一方のreplaceが
            # 相手に消費された後の実体無きパスを掴んで失敗する。
            barrier.wait(timeout=5)
            return original_replace(src, dst)

        monkeypatch.setattr(habit_projection.os, "replace", synced_replace)

        results = []
        errors = []

        def worker():
            try:
                results.append(habit_projection.export(force=True))
            except Exception as e:  # pragma: no cover - 発生したら即バグ
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert all(r["status"] == "written" for r in results), results
        assert list(projection_path.parent.glob(".cc-memory-habits-*.tmp")) == []

        state = habit_projection.read_file_state(projection_path)
        assert state.status == "ok"
        assert "並行書き込みテスト用habit" in projection_path.read_text(encoding="utf-8")


class TestKillSwitch:
    """CCM_HABITS_RULES_EXPORT=0（kill switch）のテスト"""

    def test_disabled_writes_placeholder(self, temp_db, projection_path, monkeypatch):
        monkeypatch.setattr(config, "HABITS_RULES_EXPORT_ENABLED", False)

        result = habit_projection.export()

        assert result["status"] == "written"
        content = projection_path.read_text(encoding="utf-8")
        assert "投影は停止中" in content
        assert "get_habits" in content

    def test_disabled_overwrites_existing_real_projection(self, temp_db, projection_path, monkeypatch):
        add_habit("実体のあるhabit")
        assert "投影は停止中" not in projection_path.read_text(encoding="utf-8")

        monkeypatch.setattr(config, "HABITS_RULES_EXPORT_ENABLED", False)
        result = habit_projection.export()

        assert result["status"] == "written"
        assert "投影は停止中" in projection_path.read_text(encoding="utf-8")

    def test_disabled_stops_updating_after_placeholder_write(self, temp_db, projection_path, monkeypatch):
        monkeypatch.setattr(config, "HABITS_RULES_EXPORT_ENABLED", False)
        habit_projection.export()

        result = habit_projection.export()

        assert result["status"] == "skipped"


class TestVerifyAndHeal:
    """verify_and_healのテスト"""

    def test_fresh_when_file_matches_db(self, temp_db, projection_path):
        add_habit("最新のhabit")

        conn = get_connection()
        try:
            result = habit_projection.verify_and_heal(conn)
        finally:
            conn.close()

        assert result["status"] == "fresh"

    def test_absent_file_heals(self, temp_db, projection_path):
        add_habit("再生成対象のhabit")
        projection_path.unlink()

        conn = get_connection()
        try:
            result = habit_projection.verify_and_heal(conn)
        finally:
            conn.close()

        assert result["status"] == "healed_absent"
        assert projection_path.exists()
        assert "再生成対象のhabit" in result["body"]

    def test_manually_edited_file_heals_as_stale(self, temp_db, projection_path):
        add_habit("DB側の正しいhabit")
        tampered = projection_path.read_text(encoding="utf-8") + "\n改変された行\n"
        projection_path.write_text(tampered, encoding="utf-8")

        conn = get_connection()
        try:
            result = habit_projection.verify_and_heal(conn)
        finally:
            conn.close()

        assert result["status"] == "healed_stale"
        assert "改変された行" not in projection_path.read_text(encoding="utf-8")

    def test_db_side_change_without_export_heals_as_stale(self, temp_db, projection_path):
        add_habit("最初のhabit")
        add_habit("後から追加されたhabit")

        # migration や手動SQLのような、サービス層(export呼び出し)を経由しない
        # DB変更を模す
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM habits WHERE content = ?", ("後から追加されたhabit",)
            ).fetchone()
            conn.execute(
                "UPDATE habits SET trigger_mode = 'always' WHERE id = ?", (row["id"],)
            )
            conn.commit()
        finally:
            conn.close()

        conn = get_connection()
        try:
            result = habit_projection.verify_and_heal(conn)
        finally:
            conn.close()

        assert result["status"] == "healed_stale"
        assert "後から追加されたhabit" in projection_path.read_text(encoding="utf-8")

    def test_disabled_returns_disabled_and_skips_heal(self, temp_db, projection_path, monkeypatch):
        add_habit("何かのhabit")
        monkeypatch.setattr(config, "HABITS_RULES_EXPORT_ENABLED", False)
        projection_path.unlink()

        conn = get_connection()
        try:
            result = habit_projection.verify_and_heal(conn)
        finally:
            conn.close()

        assert result["status"] == "disabled"
        assert not projection_path.exists()


class TestWritePathIntegration:
    """add_habit / update_habit / add_decisions からの投影反映テスト"""

    def test_add_habit_updates_projection_file(self, temp_db, projection_path):
        result = add_habit("add_habit経由で投影されるhabit")

        assert "rules_projection" not in result
        assert "add_habit経由で投影されるhabit" in projection_path.read_text(encoding="utf-8")

    def test_update_habit_content_updates_projection_file(self, temp_db, projection_path):
        habit_id = add_habit("元の内容")["habit_id"]

        update_habit(habit_id, content="更新後の内容")

        content = projection_path.read_text(encoding="utf-8")
        assert "更新後の内容" in content
        assert "元の内容" not in content

    def test_update_habit_active_false_updates_projection_file(self, temp_db, projection_path):
        habit_id = _add_always("無効化前は投影されるhabit")
        assert "無効化前は投影されるhabit" in projection_path.read_text(encoding="utf-8")

        update_habit(habit_id, active=False)

        assert "無効化前は投影されるhabit" not in projection_path.read_text(encoding="utf-8")

    def test_get_habits_single_fetch_does_not_touch_projection_file(self, temp_db, projection_path):
        habit_id = add_habit("参照されるだけのhabit")["habit_id"]
        mtime_before = projection_path.stat().st_mtime

        get_habits(habit_id=habit_id)

        assert projection_path.stat().st_mtime == mtime_before

    def test_add_habit_succeeds_even_when_export_raises(self, temp_db, monkeypatch):
        monkeypatch.setattr(
            habit_projection, "export",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        result = add_habit("exportが壊れていても成功するhabit")

        assert "error" not in result
        assert result["habit_id"] is not None
        assert result["rules_projection"]["status"] == "failed"
        assert "boom" in result["rules_projection"]["message"]

    def test_update_habit_succeeds_even_when_export_raises(self, temp_db, monkeypatch):
        habit_id = add_habit("後で更新するhabit")["habit_id"]
        monkeypatch.setattr(
            habit_projection, "export",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        result = update_habit(habit_id, content="更新後の内容")

        assert "error" not in result
        assert result["rules_projection"]["status"] == "failed"
        assert "boom" in result["rules_projection"]["message"]

    def test_add_decisions_habit_propagation_updates_projection_file(
        self, temp_db, projection_path, disable_embedding
    ):
        topic = add_topic(title="投影テスト用トピック", description="テスト用", tags=["domain:test"])

        result = add_decisions([
            {
                "topic_id": topic["topic_id"],
                "decision": "decision経由で追加されるhabit",
                "reason": "投影動作の確認",
                "propagate_to": {
                    "type": "habit",
                    "content": "decision伝搬経由で投影されるhabit",
                },
            },
        ])

        assert "error" not in result
        assert "rules_projection" not in result
        assert "decision伝搬経由で投影されるhabit" in projection_path.read_text(encoding="utf-8")

    def test_add_decisions_failed_propagation_does_not_export(
        self, temp_db, projection_path, disable_embedding
    ):
        topic = add_topic(title="投影テスト用トピック2", description="テスト用", tags=["domain:test"])

        add_decisions([
            {
                "topic_id": topic["topic_id"],
                "decision": "空contentでの伝搬失敗",
                "reason": "exportが走らないことの確認",
                "propagate_to": {"type": "habit", "content": ""},
            },
        ])

        assert not projection_path.exists()
