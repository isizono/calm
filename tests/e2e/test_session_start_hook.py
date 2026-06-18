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
    """OW_ROLE=worker セッションでのアクティビティ一覧抑制テスト"""

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
    """orch-managedタグ付きアクティビティの除外テスト"""

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


class TestSessionStartHookTopicGrouping:
    """topic別グルーピング表示のテスト (D#2464-2466)"""

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

    @staticmethod
    def _relate_activity_to_topic(activity_id: int, topic_id: int) -> None:
        """activity と topic の関係を relations テーブルに挿入する。
        relations_view は対称展開済みなので片方向で十分。"""
        conn = get_connection()
        try:
            # _normalize_pair に倣い (source_type < target_type) の順で挿入
            src_type, src_id = "activity", activity_id
            tgt_type, tgt_id = "topic", topic_id
            if src_type > tgt_type:
                src_type, tgt_type = tgt_type, src_type
                src_id, tgt_id = tgt_id, src_id
            conn.execute(
                "INSERT INTO relations (source_type, source_id, target_type, target_id) VALUES (?, ?, ?, ?)",
                (src_type, src_id, tgt_type, tgt_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_sections_removed(self, temp_db):
        """旧『直近作成(24h以内)』『スコアリング対象』のフラット2分割は出力されない"""
        _seed_activity("[作業] 普通の作業", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## \U0001f195 直近作成（24h以内）" not in context
        assert "## スコアリング対象" not in context

    def test_activity_grouped_under_related_topic(self, temp_db):
        """関連topicを持つアクティビティはそのtopic見出しの下に出力される"""
        topic_id = _seed_topic("検索リファインメント")
        activity_id = _seed_activity("[作業] 検索改善", status="in_progress")
        self._relate_activity_to_topic(activity_id, topic_id)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## 検索リファインメント" in context
        # トピック見出し以降にアクティビティが現れる
        topic_idx = context.index("## 検索リファインメント")
        assert f"[{activity_id}]" in context[topic_idx:]
        assert "検索改善" in context[topic_idx:]

    def test_topicless_activity_in_other_section(self, temp_db):
        """関連topicを持たないアクティビティは『その他』セクションに出る"""
        activity_id = _seed_activity("[作業] 孤立タスク", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## その他" in context
        other_idx = context.index("## その他")
        assert f"[{activity_id}]" in context[other_idx:]

    def test_topic_title_em_dash_stripped(self, temp_db):
        """topic見出しは em-dash 以降を除去した短縮版で出力される (D#2466)"""
        topic_id = _seed_topic("検索改善 — Phase 2 詳細設計")
        activity_id = _seed_activity("[作業] 検索改善実装", status="in_progress")
        self._relate_activity_to_topic(activity_id, topic_id)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        # 短縮済み見出しが出る
        assert "## 検索改善" in context
        # フルタイトルは見出しとして出ない
        assert "## 検索改善 — Phase 2 詳細設計" not in context

    def test_new_marker_inline_for_recent_activity(self, temp_db):
        """24h以内に作成されたアクティビティにはタイトル末尾に🆕がインライン付与される (D#2466)"""
        from datetime import datetime, timedelta, timezone

        activity_id = _seed_activity("[作業] 新着タスク", status="pending")
        recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self._set_created_at(activity_id, recent_time)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        # 🆕 マーカーが本文中に存在し、対象 activity の行に付与されている
        marker_line_present = False
        for line in context.splitlines():
            if f"[{activity_id}]" in line and "新着タスク" in line and "\U0001f195" in line:
                marker_line_present = True
                break
        assert marker_line_present, "24h以内作成 activity の行に🆕マーカーが付いていない"

    def test_new_marker_absent_for_old_activity(self, temp_db):
        """48h前に作成されたアクティビティには🆕マーカーが付かない"""
        from datetime import datetime, timedelta, timezone

        activity_id = _seed_activity("[作業] 古いタスク", status="pending")
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self._set_created_at(activity_id, old_time)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        for line in context.splitlines():
            if f"[{activity_id}]" in line and "古いタスク" in line:
                assert "\U0001f195" not in line, "古い activity 行に🆕マーカーが付いている"

    def test_heartbeat_section_unchanged(self, temp_db):
        """heartbeat (別セッション) セクションは topic 別グルーピングの影響を受けない (Acceptance 3)"""
        from datetime import datetime, timezone

        activity_id = _seed_activity("[作業] heartbeat作業", status="in_progress")
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

        # heartbeat セクションは従来通り
        assert "## 作業中（別セッション）" in context
        heartbeat_idx = context.index("## 作業中（別セッション）")
        assert f"[{activity_id}]" in context[heartbeat_idx:]
        # heartbeat activity は topic grouping に重複出現しない
        assert context.count(f"[{activity_id}]") == 1

    def test_numbering_continuous_across_groups(self, temp_db):
        """番号付けは複数 topic グループにまたがって連番になる"""
        topic_a = _seed_topic("グループA")
        topic_b = _seed_topic("グループB")
        a1 = _seed_activity("[作業] A-1", status="in_progress")
        a2 = _seed_activity("[作業] A-2", status="in_progress")
        b1 = _seed_activity("[作業] B-1", status="in_progress")
        self._relate_activity_to_topic(a1, topic_a)
        self._relate_activity_to_topic(a2, topic_a)
        self._relate_activity_to_topic(b1, topic_b)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        # アクティビティ番号1〜3が status_mark 付きで出現する（status_markで static guide と区別）
        for expected in ("1. ● ", "2. ● ", "3. ● "):
            assert expected in context, f"番号 '{expected}' のアクティビティ行が無い"
        # 番号4以降のアクティビティ行は出ない（合計3件）
        assert "4. ● " not in context
        assert "4. ○ " not in context

    def test_scoring_instructions_present(self, temp_db):
        """通常アクティビティが1件以上あればスコアリング指示が末尾に付く"""
        _seed_activity("[作業] スコア対象", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "# スコアリング指示" in context
