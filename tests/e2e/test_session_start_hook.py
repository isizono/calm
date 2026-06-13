"""hooks/session_start_hook.py の E2E テスト

subprocess.run で session_start_hook.py を呼び出し、stdin→stdout の入出力をテスト。
DISCUSSION_DB_PATH 環境変数でテスト用DBを指定する。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from src.db import init_database, get_connection

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


def _run_session_start_hook(
    db_path: str,
    extra_env: dict | None = None,
    env_remove: list[str] | None = None,
) -> dict:
    """session_start_hook.pyを実行してJSON出力を返す"""
    env = {**os.environ, "DISCUSSION_DB_PATH": db_path}
    # runnerのOW_ROLEを継承しない（テストの決定性確保。worker抑制テストはextra_envで明示設定する）
    env.pop("OW_ROLE", None)
    if extra_env:
        env.update(extra_env)
    if env_remove:
        for key in env_remove:
            env.pop(key, None)

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


def _seed_activity(title: str, status: str = "pending", domain: str = "test") -> int:
    """テスト用アクティビティを作成"""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
            (title, "desc", status),
        )
        activity_id = cursor.lastrowid

        # domain:タグを取得または作成
        tag_row = conn.execute(
            "SELECT id FROM tags WHERE namespace = 'domain' AND name = ?",
            (domain,),
        ).fetchone()
        if tag_row:
            tag_id = tag_row["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO tags (namespace, name) VALUES ('domain', ?)",
                (domain,),
            )
            tag_id = cursor.lastrowid

        conn.execute(
            "INSERT INTO activity_tags (activity_id, tag_id) VALUES (?, ?)",
            (activity_id, tag_id),
        )
        conn.commit()
        return activity_id
    finally:
        conn.close()


def _tag_activity_bare(activity_id: int, tag_name: str) -> None:
    """アクティビティに素タグ（namespaceなし）を付与する"""
    conn = get_connection()
    try:
        tag_row = conn.execute(
            "SELECT id FROM tags WHERE namespace = '' AND name = ?",
            (tag_name,),
        ).fetchone()
        if tag_row:
            tag_id = tag_row["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO tags (namespace, name) VALUES ('', ?)",
                (tag_name,),
            )
            tag_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO activity_tags (activity_id, tag_id) VALUES (?, ?)",
            (activity_id, tag_id),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_topic(title: str) -> int:
    """テスト用トピックを作成"""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
            (title, "desc"),
        )
        topic_id = cursor.lastrowid
        conn.commit()
        return topic_id
    finally:
        conn.close()


def _seed_habit(content: str, active: int = 1) -> int:
    """テスト用振る舞いを作成"""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO habits (content, active) VALUES (?, ?)",
            (content, active),
        )
        habit_id = cursor.lastrowid
        conn.commit()
        return habit_id
    finally:
        conn.close()


class TestSessionStartHookBasic:
    """基本的なhook出力テスト"""

    def test_output_structure(self, temp_db):
        """hook出力がhookSpecificOutput構造を持つ"""
        result = _run_session_start_hook(temp_db)

        assert "hookSpecificOutput" in result
        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "additionalContext" in result["hookSpecificOutput"]

    def test_empty_db_returns_static_guide_only(self, temp_db):
        """データが空の場合、静的なコンテキスト取得フローガイドのみ出力される"""
        # 初期データを削除
        conn = get_connection()
        try:
            conn.execute("DELETE FROM habits")
            conn.execute("DELETE FROM discussion_topics")
            conn.execute("DELETE FROM activities")
            conn.commit()
        finally:
            conn.close()

        result = _run_session_start_hook(temp_db)

        context = result["hookSpecificOutput"]["additionalContext"]
        assert "コンテキスト取得フロー" in context
        assert "補助ツール・概念" in context
        assert "# アクティビティ一覧" not in context
        assert "振る舞い" not in context


class TestSessionStartHookActivities:
    """アクティビティ一覧の注入テスト"""

    def test_activities_section_present(self, temp_db):
        """アクティブなアクティビティがあればアクティビティ一覧セクションが含まれる"""
        _seed_activity( "[作業] テスト実装", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "# アクティビティ一覧" in context
        assert "テスト実装" in context

    def test_pending_activity_shown(self, temp_db):
        """pendingアクティビティも表示される"""
        _seed_activity( "[設計] 設計作業", status="pending")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "設計作業" in context

    def test_completed_activity_not_shown(self, temp_db):
        """completedアクティビティは表示されない"""
        _seed_activity( "[作業] 完了済み", status="completed")

        # 初期振る舞いデータ削除
        conn = get_connection()
        try:
            conn.execute("DELETE FROM habits")
            conn.commit()
        finally:
            conn.close()

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "完了済み" not in context


class TestSessionStartHookTopicsRemoved:
    """トピック一覧が廃止されていることのテスト"""

    def test_topics_section_not_present(self, temp_db):
        """トピックがあってもトピック一覧セクションは表示されない"""
        _seed_topic("テストトピック")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "# トピック一覧" not in context
        assert "テストトピック" not in context


class TestSessionStartHookDuplicateActivities:
    """複数domainに属するアクティビティの重複排除テスト"""

    def _seed_activity_multi_domain(self, title: str, domains: list[str], status: str = "in_progress") -> int:
        """複数domainに属するアクティビティを作成"""
        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                (title, "desc", status),
            )
            activity_id = cursor.lastrowid

            for domain in domains:
                tag_row = conn.execute(
                    "SELECT id FROM tags WHERE namespace = 'domain' AND name = ?",
                    (domain,),
                ).fetchone()
                if tag_row:
                    tag_id = tag_row["id"]
                else:
                    cursor = conn.execute(
                        "INSERT INTO tags (namespace, name) VALUES ('domain', ?)",
                        (domain,),
                    )
                    tag_id = cursor.lastrowid

                conn.execute(
                    "INSERT INTO activity_tags (activity_id, tag_id) VALUES (?, ?)",
                    (activity_id, tag_id),
                )
            conn.commit()
            return activity_id
        finally:
            conn.close()

    def test_multi_domain_activity_shown_once(self, temp_db):
        """複数domainに属するアクティビティは1回だけ表示される"""
        activity_id = self._seed_activity_multi_domain(
            "[作業] 重複テスト", ["alpha", "beta"]
        )

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        # アクティビティIDが1回だけ出現する
        assert context.count(f"[{activity_id}]") == 1


class TestSessionStartHookHabits:
    """振る舞いの注入テスト"""

    def test_habits_section_present(self, temp_db):
        """アクティブな振る舞いがあれば振る舞いセクションが含まれる"""
        _seed_habit("テスト用振る舞い")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "# 振る舞い" in context
        assert "テスト用振る舞い" in context

    def test_inactive_habit_not_shown(self, temp_db):
        """inactive(active=0)の振る舞いは表示されない"""
        _seed_habit("無効な振る舞い", active=0)

        # 他のアクティブな振る舞いも削除
        conn = get_connection()
        try:
            conn.execute("DELETE FROM habits WHERE active = 1")
            conn.commit()
        finally:
            conn.close()

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "無効な振る舞い" not in context


class TestSessionStartHookWorkerSuppression:
    """OW_ROLE=worker セッションでのアクティビティ一覧抑制テスト（D#2409）"""

    def test_worker_session_suppresses_activity_list(self, temp_db):
        """OW_ROLE=worker時はアクティビティがあってもアクティビティ一覧セクションが出ない"""
        _seed_activity("[作業] worker抑制テスト", status="in_progress")

        result = _run_session_start_hook(temp_db, extra_env={"OW_ROLE": "worker"})
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "# アクティビティ一覧" not in context
        assert "worker抑制テスト" not in context

    def test_worker_session_keeps_habits_and_guide(self, temp_db):
        """OW_ROLE=worker時もアクティビティ以外（振る舞い・取得フローガイド）は注入される"""
        _seed_habit("worker向け振る舞い")

        result = _run_session_start_hook(temp_db, extra_env={"OW_ROLE": "worker"})
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "コンテキスト取得フロー" in context
        assert "# 振る舞い" in context
        assert "worker向け振る舞い" in context

    def test_non_worker_session_shows_activity_list(self, temp_db):
        """OW_ROLE未設定（通常セッション）ではアクティビティ一覧が出る"""
        _seed_activity("[作業] 通常表示テスト", status="in_progress")

        result = _run_session_start_hook(temp_db, env_remove=["OW_ROLE"])
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "# アクティビティ一覧" in context
        assert "通常表示テスト" in context


class TestSessionStartHookOrchManagedExclusion:
    """orch-managedタグ付きアクティビティの除外テスト（D#2410）"""

    def test_orch_managed_activity_excluded(self, temp_db):
        """orch-managedタグ付きアクティビティはアクティビティ一覧に出ない"""
        activity_id = _seed_activity("[作業] orch管理タスク", status="in_progress")
        _tag_activity_bare(activity_id, "orch-managed")

        result = _run_session_start_hook(temp_db, env_remove=["OW_ROLE"])
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "orch管理タスク" not in context
        assert f"[{activity_id}]" not in context

    def test_non_orch_managed_activity_still_shown(self, temp_db):
        """orch-managedタグのない通常アクティビティは引き続き表示される"""
        normal_id = _seed_activity("[作業] 個人タスク", status="in_progress")
        orch_id = _seed_activity("[作業] orch管理タスク", status="in_progress")
        _tag_activity_bare(orch_id, "orch-managed")

        result = _run_session_start_hook(temp_db, env_remove=["OW_ROLE"])
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "個人タスク" in context
        assert f"[{normal_id}]" in context
        assert "orch管理タスク" not in context
        assert f"[{orch_id}]" not in context

    def test_all_orch_managed_yields_no_activity_section(self, temp_db):
        """全アクティビティがorch-managedなら一覧セクション自体が出ない"""
        activity_id = _seed_activity("[作業] orch管理のみ", status="in_progress")
        _tag_activity_bare(activity_id, "orch-managed")

        # 初期振る舞いを削除してアクティビティ一覧の有無を純粋に判定
        conn = get_connection()
        try:
            conn.execute("DELETE FROM habits")
            conn.commit()
        finally:
            conn.close()

        result = _run_session_start_hook(temp_db, env_remove=["OW_ROLE"])
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "# アクティビティ一覧" not in context


class TestSessionStartHookErrorHandling:
    """エラーハンドリングのテスト"""

    def test_invalid_db_returns_empty_json(self):
        """不正なDBパスでも空JSONを出力してクラッシュしない"""
        env = {**os.environ, "DISCUSSION_DB_PATH": "/nonexistent/path/db.sqlite"}

        result = subprocess.run(
            [sys.executable, "hooks/session_start_hook.py"],
            input="{}",
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            env=env,
        )

        stdout = result.stdout.strip()
        assert stdout, "should produce some output"
        parsed = json.loads(stdout)
        # エラー時は空JSON
        assert parsed == {}


class TestSessionStartHookSyncPolicy:
    """sync_policyの注入テスト"""

    def test_sync_policy_shown_when_set(self, temp_db):
        """CCM_SYNC_POLICY設定時にsync_policyセクションが出力される"""
        result = _run_session_start_hook(
            temp_db, extra_env={"CCM_SYNC_POLICY": "PRマージ済みは自動で閉じて"}
        )
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "# sync_policy" in context
        assert "PRマージ済みは自動で閉じて" in context

    def test_sync_policy_hidden_when_unset(self, temp_db):
        """CCM_SYNC_POLICY未設定時にsync_policyセクションが出力されない"""
        result = _run_session_start_hook(
            temp_db, env_remove=["CCM_SYNC_POLICY"]
        )
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "# sync_policy" not in context

    def test_sync_policy_hidden_when_empty(self, temp_db):
        """CCM_SYNC_POLICY空文字時にsync_policyセクションが出力されない"""
        result = _run_session_start_hook(
            temp_db, extra_env={"CCM_SYNC_POLICY": ""}
        )
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "# sync_policy" not in context


class TestSessionStartHookRecentCreated:
    """直近作成（🆕マーカー）のテスト"""

    @staticmethod
    def _set_created_at(activity_id: int, created_at_iso: str) -> None:
        """アクティビティのcreated_atを指定値に上書きする"""
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE activities SET created_at = ? WHERE id = ?",
                (created_at_iso, activity_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_recent_activity_has_new_marker(self, temp_db):
        """24h以内に作成されたアクティビティに🆕マーカーが付く"""
        from datetime import datetime, timedelta, timezone

        activity_id = _seed_activity("[作業] 新規作業", status="pending")
        recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self._set_created_at(activity_id, recent_time)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "\U0001f195" in context
        assert "新規作業" in context

    def test_old_activity_has_no_new_marker(self, temp_db):
        """24h以上前に作成されたアクティビティには🆕マーカーが付かない"""
        from datetime import datetime, timedelta, timezone

        _seed_activity("[作業] 古い作業", status="pending")
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self._set_created_at(1, old_time)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "\U0001f195" not in context
        assert "古い作業" in context

    def test_heartbeat_activity_not_in_normal_list(self, temp_db):
        """is_heartbeat_activeなアクティビティはトピック別リストに混ざらず、作業中（別セッション）セクションに出る"""
        from datetime import datetime, timedelta, timezone

        activity_id = _seed_activity("[作業] heartbeat作業", status="in_progress")
        recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self._set_created_at(activity_id, recent_time)

        conn = get_connection()
        try:
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE activities SET last_heartbeat_at = ? WHERE id = ?",
                (now_iso, activity_id),
            )
            conn.commit()
        finally:
            conn.close()

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## 作業中（別セッション）" in context
        heartbeat_idx = context.index("## 作業中（別セッション）")
        assert f"[{activity_id}]" in context[heartbeat_idx:]
        assert context.count(f"[{activity_id}]") == 1


class TestSessionStartHookTopicGrouping:
    """トピック別グルーピングのテスト"""

    @staticmethod
    def _relate_activity_to_topic(activity_id: int, topic_id: int) -> None:
        """アクティビティとトピックにリレーションを張る"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO relations (source_type, source_id, target_type, target_id) VALUES (?, ?, ?, ?)",
                ("activity", activity_id, "topic", topic_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_activity_grouped_under_topic(self, temp_db):
        """トピックに関連付けられたアクティビティがトピック名の見出し配下に表示される"""
        topic_id = _seed_topic("テスト機能の実装 — 詳細説明")
        activity_id = _seed_activity("[作業] テスト実装", status="in_progress")
        self._relate_activity_to_topic(activity_id, topic_id)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## テスト機能の実装" in context
        assert "テスト実装" in context

    def test_topic_title_shortened(self, temp_db):
        """トピックタイトルの「 — 」以降が除去される"""
        topic_id = _seed_topic("短いタイトル — これは除去される長い説明")
        activity_id = _seed_activity("[作業] 作業A", status="in_progress")
        self._relate_activity_to_topic(activity_id, topic_id)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## 短いタイトル" in context
        assert "これは除去される長い説明" not in context

    def test_ungrouped_in_other_section(self, temp_db):
        """トピック未関連のアクティビティは「その他」セクションに出る"""
        _seed_activity("[作業] 孤立タスク", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## その他" in context
        assert "孤立タスク" in context

    def test_multiple_topics_each_have_section(self, temp_db):
        """複数トピックがそれぞれ独立した見出しを持つ"""
        topic_a = _seed_topic("トピックA")
        topic_b = _seed_topic("トピックB")
        aid_a = _seed_activity("[作業] 作業A", status="in_progress")
        aid_b = _seed_activity("[作業] 作業B", status="in_progress")
        self._relate_activity_to_topic(aid_a, topic_a)
        self._relate_activity_to_topic(aid_b, topic_b)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## トピックA" in context
        assert "## トピックB" in context
        assert "作業A" in context
        assert "作業B" in context
