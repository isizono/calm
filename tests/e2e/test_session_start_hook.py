"""hooks/session_start_hook.py の E2E テスト

subprocess.run で session_start_hook.py を呼び出し、stdin→stdout の入出力をテスト。
DISCUSSION_DB_PATH 環境変数でテスト用DBを指定する。
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
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
    stdin_payload: dict | None = None,
) -> dict:
    """session_start_hook.pyを実行してJSON出力を返す。

    stdin_payload を指定すると {session_id: ...} などを stdin に流し込める
    (P1-7 の自セッション照合テスト用)。
    """
    env = {**os.environ, "DISCUSSION_DB_PATH": db_path}
    # runnerのOW_ROLEを継承しない（テストの決定性確保。worker抑制テストはextra_envで明示設定する）
    env.pop("OW_ROLE", None)
    if extra_env:
        env.update(extra_env)
    if env_remove:
        for key in env_remove:
            env.pop(key, None)

    payload_str = "{}" if stdin_payload is None else json.dumps(stdin_payload)
    result = subprocess.run(
        [sys.executable, "hooks/session_start_hook.py"],
        input=payload_str,
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


def _mark_orch_managed(activity_id: int) -> None:
    """アクティビティの orch_managed カラムを 1 に設定する"""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE activities SET orch_managed = 1 WHERE id = ?",
            (activity_id,),
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
        assert context.count(f"(#{activity_id})") == 1


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
    """orch_managed=1 アクティビティの除外テスト"""

    def test_orch_managed_activity_excluded(self, temp_db):
        """orch_managed=1 のアクティビティはアクティビティ一覧に出ない"""
        activity_id = _seed_activity("[作業] orch管理タスク", status="in_progress")
        _mark_orch_managed(activity_id)

        result = _run_session_start_hook(temp_db, env_remove=["OW_ROLE"])
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "orch管理タスク" not in context
        assert f"(#{activity_id})" not in context

    def test_non_orch_managed_activity_still_shown(self, temp_db):
        """orch_managed=0 の通常アクティビティは引き続き表示される"""
        normal_id = _seed_activity("[作業] 個人タスク", status="in_progress")
        orch_id = _seed_activity("[作業] orch管理タスク", status="in_progress")
        _mark_orch_managed(orch_id)

        result = _run_session_start_hook(temp_db, env_remove=["OW_ROLE"])
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "個人タスク" in context
        assert f"(#{normal_id})" in context
        assert "orch管理タスク" not in context
        assert f"(#{orch_id})" not in context

    def test_orch_managed_tag_without_column_is_still_shown(self, temp_db):
        """旧 orch-managed 素タグだけ付いていて orch_managed カラムが 0 のアクティビティは
        新仕様では普通に表示される（タグ判定は撤廃済み）。"""
        activity_id = _seed_activity("[作業] レガシータグ", status="in_progress")
        _tag_activity_bare(activity_id, "orch-managed")

        result = _run_session_start_hook(temp_db, env_remove=["OW_ROLE"])
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "レガシータグ" in context
        assert f"(#{activity_id})" in context

    def test_all_orch_managed_yields_no_activity_section(self, temp_db):
        """全アクティビティが orch_managed=1 なら一覧セクション自体が出ない"""
        activity_id = _seed_activity("[作業] orch管理のみ", status="in_progress")
        _mark_orch_managed(activity_id)

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
    """topic別グルーピング表示のテスト"""

    @staticmethod
    def _relate_activity_to_topic(activity_id: int, topic_id: int) -> None:
        """activity と topic の関係を relations テーブルに挿入する。
        _normalize_pair の正規化順 (source_type < target_type 辞書順) では
        'activity' < 'topic' のためスワップは発生せず、そのまま挿入できる。
        relations_view は対称展開済みなので片方向で十分。"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO relations (source_type, source_id, target_type, target_id) VALUES (?, ?, ?, ?)",
                ("activity", activity_id, "topic", topic_id),
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

    def _seed_tier4_activity(self, title: str, status: str = "pending") -> int:
        """階層 4（updated_at 30日以内かつ 24h より古く、in_progress でない）に落ちる
        アクティビティを作成する"""
        from datetime import datetime, timedelta, timezone

        activity_id = _seed_activity(title, status=status)
        old_iso = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        _set_created_at(activity_id, old_iso)
        _set_updated_at(activity_id, old_iso)
        return activity_id

    def test_activity_grouped_under_related_topic(self, temp_db):
        """階層 4 で関連 topic を持つアクティビティはその topic 見出しの下に出力される"""
        topic_id = _seed_topic("検索リファインメント")
        activity_id = self._seed_tier4_activity("[作業] 検索改善")
        self._relate_activity_to_topic(activity_id, topic_id)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## 検索リファインメント" in context
        topic_idx = context.index("## 検索リファインメント")
        assert f"(#{activity_id})" in context[topic_idx:]
        assert "検索改善" in context[topic_idx:]

    def test_tier4_line_format_no_number_no_status_marker(self, temp_db):
        """階層 4 の行は『- タイトル』形式（番号なし・status マーカー(●/○) なし・meta 行なし）"""
        topic_id = _seed_topic("tier4 test topic")
        activity_id = self._seed_tier4_activity("[作業] tier4 タスク")
        self._relate_activity_to_topic(activity_id, topic_id)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        target_line: str | None = None
        for line in context.splitlines():
            if f"(#{activity_id})" in line:
                target_line = line
                break
        assert target_line is not None, f"activity (#{activity_id}) の行が見つからない"
        stripped = target_line.lstrip()
        assert stripped.startswith("- "), (
            f"tier 4 行が番号付きになっている: {target_line!r}"
        )
        assert "●" not in target_line and "○" not in target_line, (
            f"tier 4 に status マーカーが混入: {target_line!r}"
        )

    def test_topicless_activity_in_other_section(self, temp_db):
        """階層 4 で関連 topic を持たないアクティビティは『その他』セクションに出る"""
        activity_id = self._seed_tier4_activity("[作業] 孤立タスク")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## その他" in context
        other_idx = context.index("## その他")
        assert f"(#{activity_id})" in context[other_idx:]

    def test_topic_title_em_dash_stripped(self, temp_db):
        """topic 見出しは em-dash 以降を除去した短縮版で出力される"""
        topic_id = _seed_topic("検索改善 — Phase 2 詳細設計")
        activity_id = self._seed_tier4_activity("[作業] 検索改善実装")
        self._relate_activity_to_topic(activity_id, topic_id)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## 検索改善" in context
        assert "## 検索改善 — Phase 2 詳細設計" not in context

    def test_new_marker_inline_for_recent_activity(self, temp_db):
        """24h以内に作成されたアクティビティにはタイトル末尾に🆕がインライン付与される"""
        from datetime import datetime, timedelta, timezone

        activity_id = _seed_activity("[作業] 新着タスク", status="pending")
        recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        _set_created_at(activity_id, recent_time)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        # 🆕 マーカーが本文中に存在し、対象 activity の行に付与されている
        marker_line_present = False
        for line in context.splitlines():
            if f"(#{activity_id})" in line and "新着タスク" in line and "\U0001f195" in line:
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
        _set_created_at(activity_id, old_time)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        for line in context.splitlines():
            if f"(#{activity_id})" in line and "古いタスク" in line:
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
        assert f"(#{activity_id})" in context[heartbeat_idx:]
        # heartbeat activity は topic grouping に重複出現しない
        assert context.count(f"(#{activity_id})") == 1

    def test_numbering_continuous_in_priority_tier(self, temp_db):
        """階層 2『優先』は flat リストで連番になる"""
        a1 = _seed_activity("[作業] A-1", status="in_progress")
        a2 = _seed_activity("[作業] A-2", status="in_progress")
        b1 = _seed_activity("[作業] B-1", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        for expected in ("1. ● ", "2. ● ", "3. ● "):
            assert expected in context, f"番号 '{expected}' のアクティビティ行が無い"
        assert "4. ● " not in context
        assert "4. ○ " not in context

    def test_deterministic_render_notice_present(self, temp_db):
        """通常アクティビティが1件以上あれば末尾固定文が付く"""
        _seed_activity("[作業] 通常タスク", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "決定論的に組み立てた表示用 markdown" in context
        assert "再フォーマットや優先順の再評価をせず" in context

    def test_scoring_instructions_absent(self, temp_db):
        """旧スコアリング指示文は出力されない"""
        _seed_activity("[作業] タスク", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "# スコアリング指示" not in context
        assert "優先度の高い上位5件を選び" not in context


def _set_heartbeat(activity_id: int, session_id: str | None) -> None:
    """activity の heartbeat を「今」に更新し session_id を同梱する"""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE activities SET last_heartbeat_at = ?, last_heartbeat_session_id = ? WHERE id = ?",
            (now_iso, session_id, activity_id),
        )
        conn.commit()
    finally:
        conn.close()


class TestSessionStartHookSelfSessionHeartbeat:
    """P1-7: 自セッション自身の heartbeat を「別セッション扱い」しない"""

    def test_self_session_heartbeat_not_in_other_session_block(self, temp_db):
        """stdin session_id と一致する heartbeat は「## 作業中（別セッション）」に出ない"""
        activity_id = _seed_activity("[作業] 自セッション中", status="in_progress")
        _set_heartbeat(activity_id, session_id="sess-self")

        result = _run_session_start_hook(
            temp_db, stdin_payload={"session_id": "sess-self"}
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        # 「別セッション」ブロック自体が出ないか、出ても対象 activity が含まれない
        if "## 作業中（別セッション）" in context:
            other_block_start = context.index("## 作業中（別セッション）")
            # 直後セクションまでで切り出す
            tail = context[other_block_start:]
            next_section = tail.find("\n## ", 1)
            other_block = tail if next_section == -1 else tail[:next_section]
            assert f"(#{activity_id})" not in other_block, (
                "自セッションの heartbeat が「作業中（別セッション）」に出てしまっている"
            )

    def test_other_session_heartbeat_in_other_session_block(self, temp_db):
        """別 session_id の heartbeat は引き続き「## 作業中（別セッション）」に出る"""
        activity_id = _seed_activity("[作業] 別セッション中", status="in_progress")
        _set_heartbeat(activity_id, session_id="sess-other")

        result = _run_session_start_hook(
            temp_db, stdin_payload={"session_id": "sess-self"}
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## 作業中（別セッション）" in context
        heartbeat_idx = context.index("## 作業中（別セッション）")
        assert f"(#{activity_id})" in context[heartbeat_idx:], (
            "他セッション heartbeat が「作業中（別セッション）」に出ていない"
        )

    def test_null_session_id_falls_back_to_other_session(self, temp_db):
        """last_heartbeat_session_id=NULL（カラム導入前データ）は従来通り別セッション扱い"""
        activity_id = _seed_activity("[作業] 旧データ", status="in_progress")
        _set_heartbeat(activity_id, session_id=None)

        result = _run_session_start_hook(
            temp_db, stdin_payload={"session_id": "sess-self"}
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## 作業中（別セッション）" in context
        heartbeat_idx = context.index("## 作業中（別セッション）")
        assert f"(#{activity_id})" in context[heartbeat_idx:]

    def test_no_stdin_session_id_keeps_other_session_block(self, temp_db):
        """stdin に session_id が無い場合は照合不能 → 従来通り別セッション扱い"""
        activity_id = _seed_activity("[作業] sid不明", status="in_progress")
        _set_heartbeat(activity_id, session_id="sess-anything")

        # 引数省略で stdin_payload=None → {}
        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## 作業中（別セッション）" in context
        heartbeat_idx = context.index("## 作業中（別セッション）")
        assert f"(#{activity_id})" in context[heartbeat_idx:]


def _set_updated_at(activity_id: int, updated_at_iso: str) -> None:
    """アクティビティのupdated_atを指定値に上書きする"""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE activities SET updated_at = ? WHERE id = ?",
            (updated_at_iso, activity_id),
        )
        conn.commit()
    finally:
        conn.close()


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


def _add_pin_activity(source_type: str, source_id, target_activity_id: int) -> None:
    """pins テーブルに source → target_activity のpinを挿入する"""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO pins (source_type, source_id, target_type, target_id) "
            "VALUES (?, ?, 'activity', ?)",
            (source_type, source_id, target_activity_id),
        )
        conn.commit()
    finally:
        conn.close()


def _add_activity_dependency(dependent_id: int, dependency_id: int) -> None:
    """activity_dependencies テーブルに依存関係を挿入する"""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO activity_dependencies (dependent_id, dependency_id) "
            "VALUES (?, ?)",
            (dependent_id, dependency_id),
        )
        conn.commit()
    finally:
        conn.close()


class TestSessionStartHook4TierDashboard:
    """4 階層ダッシュボード（優先 / 直近作成 / その他）のテスト"""

    def _seed_old_activity(
        self,
        title: str,
        status: str = "pending",
        days_ago: int = 3,
    ) -> int:
        """created_at / updated_at を N 日前に設定した activity を作成する"""
        from datetime import datetime, timedelta, timezone

        activity_id = _seed_activity(title, status=status)
        iso = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        _set_created_at(activity_id, iso)
        _set_updated_at(activity_id, iso)
        return activity_id

    def test_tier_sections_in_expected_order(self, temp_db):
        """階層 1（別セッション）→ 階層 2（優先）→ 階層 3（直近作成）→ 階層 4 の順で出る"""
        from datetime import datetime, timedelta, timezone

        heartbeat_id = _seed_activity("[作業] heartbeat別", status="in_progress")
        _set_heartbeat(heartbeat_id, session_id="sess-other")

        priority_id = _seed_activity("[作業] 優先タスク", status="in_progress")

        recent_id = _seed_activity("[作業] 新着タスク", status="pending")
        recent_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        _set_created_at(recent_id, recent_iso)

        other_id = self._seed_old_activity("[作業] 過去タスク")

        result = _run_session_start_hook(
            temp_db, stdin_payload={"session_id": "sess-self"}
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        idx_tier1 = context.index("## 作業中（別セッション）")
        idx_tier2 = context.index("## 優先")
        idx_tier3 = context.index("## 直近作成（24h以内）")
        idx_tier4 = context.index("## その他")

        assert idx_tier1 < idx_tier2 < idx_tier3 < idx_tier4
        assert f"(#{heartbeat_id})" in context[idx_tier1:idx_tier2]
        assert f"(#{priority_id})" in context[idx_tier2:idx_tier3]
        assert f"(#{recent_id})" in context[idx_tier3:idx_tier4]
        assert f"(#{other_id})" in context[idx_tier4:]

    def test_pinned_pending_activity_in_priority_tier(self, temp_db):
        """pinned な pending activity は階層 2『優先』に入る。📌 マーカー付き"""
        pinned_id = self._seed_old_activity("[作業] 保留中の優先", status="pending")
        source_topic_id = _seed_topic("pin source topic")
        _add_pin_activity("topic", source_topic_id, pinned_id)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## 優先" in context
        idx_tier2 = context.index("## 優先")
        idx_next = context.find("\n## ", idx_tier2 + 1)
        tier2_block = context[idx_tier2:] if idx_next == -1 else context[idx_tier2:idx_next]

        assert f"(#{pinned_id})" in tier2_block, "pinned pending が階層 2 に入っていない"
        assert "\U0001f4cc" in tier2_block, "📌 マーカーが階層 2 に出ていない"

        for line in tier2_block.splitlines():
            if f"(#{pinned_id})" in line:
                assert "\U0001f4cc" in line, "対象 pinned 行に 📌 が付いていない"
                break

    def test_tier2_pinned_precedes_newer_in_progress(self, temp_db):
        """階層 2 内で pinned は updated_at が古くても新しい in_progress より上位に来る"""
        old_pinned_id = self._seed_old_activity(
            "[作業] 古い pinned", status="pending", days_ago=3
        )
        source_topic_id = _seed_topic("pin source topic")
        _add_pin_activity("topic", source_topic_id, old_pinned_id)

        new_ip_id = _seed_activity("[作業] 新しい進行中", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        idx_tier2 = context.index("## 優先")
        idx_next = context.find("\n## ", idx_tier2 + 1)
        tier2_block = context[idx_tier2:] if idx_next == -1 else context[idx_tier2:idx_next]

        old_pos = tier2_block.index(f"(#{old_pinned_id})")
        new_pos = tier2_block.index(f"(#{new_ip_id})")
        assert old_pos < new_pos, (
            "pinned が新しい in_progress より下位に出ている（pinned-first 順序違反）"
        )

    def test_tier3_omitted_when_all_activities_are_old(self, temp_db):
        """階層 3 の対象 activity が 0 件（全て 24h より古い作成）のとき、セクション見出しごと省略"""
        _seed_activity("[作業] 進行中タスク", status="in_progress")
        self._seed_old_activity("[作業] 古い pending 1", status="pending", days_ago=3)
        self._seed_old_activity("[作業] 古い pending 2", status="pending", days_ago=5)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## 直近作成（24h以内）" not in context
        assert "## 優先" in context

    def test_recent_created_tier_omitted_when_empty(self, temp_db):
        """階層 3 の対象 activity が 0 件のとき、セクション見出しごと省略される"""
        _seed_activity("[作業] 通常タスク", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## 直近作成（24h以内）" not in context

    def test_tier4_excludes_activity_older_than_30_days(self, temp_db):
        """階層 4 は updated_at 30 日以上の activity を除外する"""
        excluded_id = self._seed_old_activity(
            "[作業] 古すぎタスク", days_ago=45
        )

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert f"(#{excluded_id})" not in context

    def test_meta_line_shown_only_when_blocked_by_present(self, temp_db):
        """階層 2/3 で blocked_by 未解決の依存があるときのみ meta 行が出る"""
        blocker_id = _seed_activity("[作業] blocker", status="pending")
        blocked_id = _seed_activity("[作業] blocked", status="in_progress")
        _add_activity_dependency(blocked_id, blocker_id)

        plain_id = _seed_activity("[作業] plain", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        lines = context.splitlines()
        blocked_line_idx = None
        plain_line_idx = None
        for i, line in enumerate(lines):
            if f"(#{blocked_id})" in line and line.lstrip().startswith(
                ("1.", "2.", "3.", "4.", "5.")
            ):
                blocked_line_idx = i
            if f"(#{plain_id})" in line and line.lstrip().startswith(
                ("1.", "2.", "3.", "4.", "5.")
            ):
                plain_line_idx = i
        assert blocked_line_idx is not None, "blocked activity の番号付き行が見つからない"
        assert plain_line_idx is not None, "plain activity の番号付き行が見つからない"

        assert lines[blocked_line_idx + 1].strip().startswith("blocked_by:")
        assert "blocker" in lines[blocked_line_idx + 1]

        next_after_plain = lines[plain_line_idx + 1] if plain_line_idx + 1 < len(lines) else ""
        assert not next_after_plain.strip().startswith("blocked_by:"), (
            "blocked_by 無しの activity に meta 行が出てしまっている"
        )

    def test_pinned_overflow_and_stale_still_appears(self, temp_db):
        """pinned が階層 2 の上限(5)を溢れ、かつ updated_at 30 日超でも消えず階層 4 に残る"""
        source_topic_id = _seed_topic("pin source topic")

        # 階層 2 の上限 5 件を埋める pinned（updated_at は新しめ、created_at は
        # 24h より前で階層 3 対象外）
        for i in range(5):
            filler_id = self._seed_old_activity(
                f"[作業] pinned filler {i}", status="pending", days_ago=2
            )
            _add_pin_activity("topic", source_topic_id, filler_id)

        # updated_at が最も古く（45 日前）階層 2 上位 5 件から溢れ、
        # かつ階層 4 の 30 日フィルタにも掛かる pinned
        stale_id = self._seed_old_activity(
            "[作業] 消えないはずの pinned", status="pending", days_ago=45
        )
        _add_pin_activity("topic", source_topic_id, stale_id)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        # どの階層からも脱落せず、ダッシュボードに残っている
        assert f"(#{stale_id})" in context, (
            "pinned が上限溢れ＋30日超で消えている（脱落バグ回帰）"
        )

        # 階層 2『優先』には上位 5 件のみ入り、最古の stale pinned は溢れている
        idx_tier2 = context.index("## 優先")
        idx_next = context.find("\n## ", idx_tier2 + 1)
        tier2_block = (
            context[idx_tier2:] if idx_next == -1 else context[idx_tier2:idx_next]
        )
        assert f"(#{stale_id})" not in tier2_block, (
            "stale pinned が階層 2 の上限を無視して入っている"
        )

        # 溢れた pinned は 📌 付きで下位階層に残る
        stale_line = None
        for line in context.splitlines():
            if f"(#{stale_id})" in line:
                stale_line = line
                break
        assert stale_line is not None
        assert "\U0001f4cc" in stale_line, "残存 pinned 行に 📌 が付いていない"

    def test_pinned_heartbeat_activity_shows_pin_marker_in_tier1(self, temp_db):
        """pinned かつ別セッション heartbeat の activity は階層 1 で 📌 付きで出る"""
        heartbeat_id = _seed_activity("[作業] pinned heartbeat", status="in_progress")
        _set_heartbeat(heartbeat_id, session_id="sess-other")
        source_topic_id = _seed_topic("pin source topic")
        _add_pin_activity("topic", source_topic_id, heartbeat_id)

        result = _run_session_start_hook(
            temp_db, stdin_payload={"session_id": "sess-self"}
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## 作業中（別セッション）" in context
        idx_tier1 = context.index("## 作業中（別セッション）")
        idx_next = context.find("\n## ", idx_tier1 + 1)
        tier1_block = (
            context[idx_tier1:] if idx_next == -1 else context[idx_tier1:idx_next]
        )

        target_line = None
        for line in tier1_block.splitlines():
            if f"(#{heartbeat_id})" in line:
                target_line = line
                break
        assert target_line is not None, "pinned heartbeat が階層 1 に出ていない"
        assert "\U0001f4cc" in target_line, "階層 1 の pinned 行に 📌 が付いていない"

    def test_worker_session_returns_empty_activities_section(self, temp_db):
        """worker session ではアクティビティセクションが空になる"""
        _seed_activity("[作業] worker下タスク", status="in_progress")

        result = _run_session_start_hook(temp_db, extra_env={"OW_ROLE": "worker"})
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "# アクティビティ一覧" not in context
        assert "## 優先" not in context
        assert "## その他" not in context
