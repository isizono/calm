"""hooks/session_start_hook.py の E2E テスト

subprocess.run で session_start_hook.py を呼び出し、stdin→stdout の入出力をテスト。
DISCUSSION_DB_PATH 環境変数でテスト用DBを指定する。

hookはhabits rules投影ファイル（既定 ~/.claude/rules/cc-memory-habits.md）を
verify_and_heal経由で検証・書き込みしうる。テストプロセスとhookは別プロセス
なのでconftestのmonkeypatch（_isolate_habits_rules_projection）は伝播しない。
_run_session_start_hookが呼び出しごとに使い捨てのCALM_HABITS_RULES_PATHを
強制注入し、実ファイルへの書き込みを防ぐ。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.db import init_database, get_connection
from src.env_compat import CANONICAL_PREFIX, env_names

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
    habits_rules_path: str | None = None,
) -> dict:
    """session_start_hook.pyを実行してJSON出力を返す。

    stdin_payload を指定すると {session_id: ...} などを stdin に流し込める
    (P1-7 の自セッション照合テスト用)。

    habits_rules_path 未指定時は呼び出しごとの使い捨てディレクトリを生成し、
    CALM_HABITS_RULES_PATH で強制的に隔離する（実ファイルへの書き込み防止）。
    ファイル修復の検証等でパスを固定したいテストは明示的に渡すこと。
    """
    env = {**os.environ, "DISCUSSION_DB_PATH": db_path}
    # runnerのOW_ROLEを継承しない（テストの決定性確保。残存env検証テストはextra_envで明示設定する）
    env.pop("OW_ROLE", None)

    cleanup_dir: str | None = None
    if habits_rules_path is None:
        cleanup_dir = tempfile.mkdtemp(prefix="ccm-habits-rules-")
        habits_rules_path = str(Path(cleanup_dir) / "cc-memory-habits.md")
    env["CALM_HABITS_RULES_PATH"] = habits_rules_path

    if extra_env:
        env.update(extra_env)
    if env_remove:
        for key in env_remove:
            # CALM_ 系は旧名フォールバックが効くため、CALM_ 名だけ消しても
            # 呼び出し元の環境に残った CCM_ / CC_MEMORY_ 名から値が復活する。
            # 新旧まとめて落とす。それ以外の環境変数はその名前だけ落とす。
            names = env_names(key) if key.startswith(CANONICAL_PREFIX) else (key,)
            for name in names:
                env.pop(name, None)

    try:
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
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


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


def _seed_signal(kind: str, summary: str, source: str = "tool:test") -> int:
    """テスト用シグナル（status='new'）を作成する"""
    from src.services.signal_service import record_signal

    result = record_signal(kind, summary, source=source)
    return result["id"]


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


def _set_habit_trigger_mode(habit_id: int, trigger_mode: str, description: str = "") -> None:
    """テスト用振る舞いのtrigger_mode/descriptionを更新する"""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE habits SET trigger_mode = ?, description = ? WHERE id = ?",
            (trigger_mode, description, habit_id),
        )
        conn.commit()
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

    def test_empty_db_returns_empty_context(self, temp_db):
        """データが空の場合、ヘッダを持つ動的セクション（アクティビティ一覧見出し・
        振る舞い）は出力されない。アクティビティ一覧の固定ナビ（check_in導線）は
        activityが0件でも常に出力される"""
        # 初期データを削除
        conn = get_connection()
        try:
            conn.execute("DELETE FROM habits")
            conn.execute("DELETE FROM discussion_topics")
            conn.execute("DELETE FROM activities")
            conn.commit()
        finally:
            conn.close()

        result = _run_session_start_hook(temp_db, env_remove=["CALM_SYNC_POLICY"])

        context = result["hookSpecificOutput"]["additionalContext"]
        assert "check_in（なければ作成 — activity-start）" in context
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

    def test_pending_pinned_activity_shown(self, temp_db):
        """pinned な pending アクティビティは階層2条件を満たし個別表示される"""
        activity_id = _seed_activity("[設計] 設計作業", status="pending")
        topic_id = _seed_topic("pin source")
        _add_pin_activity("topic", topic_id, activity_id)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "設計作業" in context

    def test_pending_non_pinned_activity_hidden_and_counted_in_nav(self, temp_db):
        """pinnedでもin_progressでもないpendingアクティビティは階層2に入らず、
        固定ナビの未表示件数句としてのみ反映される（階層1・2とも0件のためヘッダも出ない）"""
        _seed_activity("[設計] 設計作業", status="pending")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "# アクティビティ一覧" not in context
        assert "設計作業" not in context
        assert "未表示のアクティビティ1件" in context

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

    def test_intelligently_habit_degrades_to_count_only(self, temp_db):
        """trigger_mode='intelligently'の振る舞いは、投影ファイル未生成時の縮退フォール
        バックではタイトルも全文も出ず、件数1行にとどまる（全文はrulesファイル経由で
        配信され、hookのフォールバックは全文9,500字級の再注入を避ける設計のため）"""
        habit_id = _seed_habit("intelligently層の本文全部が長い振る舞い内容")
        _set_habit_trigger_mode(habit_id, "intelligently", description="短い要旨")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "短い要旨" not in context
        assert "intelligently層の本文全部が長い振る舞い内容" not in context
        assert f"habit_id={habit_id}" not in context
        assert "他の振る舞い: 1件" in context

    def test_always_habit_shown_in_full(self, temp_db):
        """trigger_mode='always'（既定）の振る舞いは全文が出る"""
        _seed_habit("always層の振る舞い全文")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "always層の振る舞い全文" in context


class TestSessionStartHookHabitsProjectionCutover:
    """habits rules投影ファイルの鮮度に応じた、hookの検証+縮退注入のテスト

    正の配信経路は~/.claude/rules配下の自動生成ファイル（launch時読み込み）で
    あり、本hookはverify_and_heal経由でそのファイルの鮮度を検証するだけの
    縮退面になる。各テストはhabits_rules_pathを固定してファイル状態を
    テスト間で制御する。
    """

    def test_fresh_projection_yields_no_habits_injection(self, temp_db, tmp_path):
        """投影ファイルがDB内容と一致(fresh)なら、hookはhabitsセクションを注入しない
        （habitsはrulesファイル経由で既にコンテキストへ読み込まれている前提のため）"""
        _seed_habit("fresh判定用の振る舞い")
        habits_path = tmp_path / "cc-memory-habits.md"

        # 1回目: ファイル不在(absent)のため生成される
        _run_session_start_hook(temp_db, habits_rules_path=str(habits_path))

        # 2回目: DBに変化なし → ファイルはfreshのはず
        result = _run_session_start_hook(temp_db, habits_rules_path=str(habits_path))
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "振る舞い" not in context

    def test_stale_projection_yields_single_line_and_heals_file(self, temp_db, tmp_path):
        """投影ファイルがDBより古い(stale)場合、1行通知のみを注入しファイルを修復する"""
        _seed_habit("stale判定用の振る舞い1")
        habits_path = tmp_path / "cc-memory-habits.md"
        _run_session_start_hook(temp_db, habits_rules_path=str(habits_path))  # 初回生成

        # サービス層のexport経路を経由せずDBだけ変更し、ファイルを古い状態のまま残す
        _seed_habit("stale判定用の振る舞い2")

        result = _run_session_start_hook(temp_db, habits_rules_path=str(habits_path))
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "次回セッション起動から" in context
        assert "stale判定用の振る舞い1" not in context
        assert "stale判定用の振る舞い2" not in context
        assert "stale判定用の振る舞い2" in habits_path.read_text(encoding="utf-8")

    def test_absent_projection_degrades_to_always_full_and_manifest_count(self, temp_db, tmp_path):
        """投影ファイル不在(absent)時は、always層は全文、intelligently層は件数1行に
        減格したうえでファイルを新規生成する"""
        _seed_habit("absent時のalways全文振る舞い")
        intelligently_id = _seed_habit("absent時のintelligently本文（全文は出ないはず）")
        _set_habit_trigger_mode(intelligently_id, "intelligently", description="短い要旨")

        habits_path = tmp_path / "cc-memory-habits.md"
        assert not habits_path.exists()

        result = _run_session_start_hook(temp_db, habits_rules_path=str(habits_path))
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "absent時のalways全文振る舞い" in context
        assert "absent時のintelligently本文（全文は出ないはず）" not in context
        assert "短い要旨" not in context
        assert "他の振る舞い: 1件" in context
        assert habits_path.exists()

    def test_kill_switch_degrades_without_being_treated_as_fresh(self, temp_db, tmp_path):
        """kill switch（CALM_HABITS_RULES_EXPORT=0）中はfresh扱いにせず、
        always全文+件数1行の縮退注入にフォールバックする（プレースホルダ運用中の
        ファイルがそのまま読まれ続ける事態を避けるため）"""
        _seed_habit("kill switch下のalways振る舞い")
        habits_path = tmp_path / "cc-memory-habits.md"

        result = _run_session_start_hook(
            temp_db,
            habits_rules_path=str(habits_path),
            extra_env={"CALM_HABITS_RULES_EXPORT": "0"},
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "kill switch下のalways振る舞い" in context

    def test_absent_write_failure_yields_failure_fallback_not_silent_success(self, temp_db, tmp_path):
        """投影ファイルが不在(absent)で、かつ修復の書き込み自体が失敗するケース
        (failed_absent)。投影先の親パスコンポーネントを通常ファイルにすることで
        os.replaceベースの書き込みを構造的に失敗させる（root権限でも回避できない
        FS制約であり、chmodベースの権限テストと違って実行ユーザーに依存しない）。
        healed_absentと違い実際にはファイルは書けていないが、常に安全側の
        always層全文フォールバックが注入され、例外で握りつぶされないことを確認する"""
        _seed_habit("absent書き込み失敗時に注入されるべきalways全文")
        blocker_file = tmp_path / "blocker"
        blocker_file.write_text("this is a file, not a directory", encoding="utf-8")
        # 親ディレクトリ相当のパスコンポーネントが既存の通常ファイルなので、
        # habit_projection._write 内の path.parent.mkdir が必ず失敗する
        habits_path = blocker_file / "cc-memory-habits.md"

        result = _run_session_start_hook(temp_db, habits_rules_path=str(habits_path))
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "absent書き込み失敗時に注入されるべきalways全文" in context
        assert not habits_path.exists()

    def test_compact_source_reinjects_habits_even_when_fresh(self, temp_db, tmp_path):
        """マージ前ゲート対応確認: SessionStart(source=compact)時は、compact後に
        rulesファイル内容がコンテキストへ保持されるかの実機検証が未了なため、
        投影ファイルがfresh（通常なら注入なし）でも安全側に倒してhabitsの
        always層全文フォールバックを再注入する"""
        _seed_habit("compact再注入対象の振る舞い")
        habits_path = tmp_path / "cc-memory-habits.md"

        # 1回目: absentからのheal。2回目以降がfresh判定になる前提を作る
        _run_session_start_hook(temp_db, habits_rules_path=str(habits_path))

        # 2回目: DBに変化なし（freshのはず）だが source=compact を明示
        result = _run_session_start_hook(
            temp_db,
            habits_rules_path=str(habits_path),
            stdin_payload={"source": "compact"},
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "compact再注入対象の振る舞い" in context

    def test_non_compact_source_still_yields_no_injection_when_fresh(self, temp_db, tmp_path):
        """source=startup等（compact以外）では従来通り、freshなら注入なし"""
        _seed_habit("fresh判定確認用の振る舞い")
        habits_path = tmp_path / "cc-memory-habits.md"

        _run_session_start_hook(temp_db, habits_rules_path=str(habits_path))

        result = _run_session_start_hook(
            temp_db,
            habits_rules_path=str(habits_path),
            stdin_payload={"source": "startup"},
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "振る舞い" not in context

    def test_verify_and_heal_exception_does_not_break_other_sections(self, temp_db, tmp_path):
        """habitsセクションが例外を投げても、他セクション（アクティビティ一覧等）は
        引き続き返る（builders統一IFのセクション単位try/exceptの回帰確認）"""
        _seed_activity("[作業] habits例外時も表示される", status="in_progress")

        conn = get_connection()
        try:
            conn.execute("DROP TABLE habits")
            conn.commit()
        finally:
            conn.close()

        habits_path = tmp_path / "cc-memory-habits.md"
        result = _run_session_start_hook(temp_db, habits_rules_path=str(habits_path))
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "habits例外時も表示される" in context
        assert "振る舞い" not in context

    def test_standard_fixture_context_within_budget_on_fresh_habits_path(self, temp_db, tmp_path):
        """habits投影ファイルがfresh（配信済み）な標準fixtureでも、additionalContext
        合計が1,900字以下である。fresh経路はhabitsセクションが0字になるため、既存の
        絶対経路（初回=absent）の回帰テストとは別に、fresh判定自体が字数超過を
        起こさないことを確認する"""
        for i in range(3):
            _seed_activity(f"[作業] 通常タスク{i}", status="in_progress")

        pinned_id = _seed_activity("[設計] pinned設計作業", status="pending")
        topic_id = _seed_topic("pin source")
        _add_pin_activity("topic", topic_id, pinned_id)

        for i in range(2):
            _seed_habit(f"標準的な長さの振る舞い内容その{i}")

        habits_path = tmp_path / "cc-memory-habits.md"
        # 1回目: absentからのheal。DB状態を投影ファイルへ反映させる
        _run_session_start_hook(
            temp_db, habits_rules_path=str(habits_path), env_remove=["CALM_SYNC_POLICY"]
        )

        # 2回目: freshのはず
        result = _run_session_start_hook(
            temp_db, habits_rules_path=str(habits_path), env_remove=["CALM_SYNC_POLICY"]
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "振る舞い" not in context
        assert len(context) <= 1900, (
            f"additionalContextが1,900字を超えている（実測{len(context)}字）"
        )


class TestSessionStartHookOwRoleEnvIgnored:
    """残存する OW_ROLE 環境変数がhook挙動に影響しないことのテスト"""

    def test_no_ow_role_env_shows_activity_list(self, temp_db):
        """OW_ROLE未設定（通常セッション）ではアクティビティ一覧が出る"""
        _seed_activity("[作業] 通常表示テスト", status="in_progress")

        result = _run_session_start_hook(temp_db, env_remove=["OW_ROLE"])
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "# アクティビティ一覧" in context
        assert "通常表示テスト" in context

    def test_stale_ow_role_env_still_shows_activity_list(self, temp_db):
        """OW_ROLE=workerが環境に残存していてもアクティビティ一覧は注入される"""
        _seed_activity("[作業] 残存env下タスク", status="in_progress")
        _seed_habit("残存env下振る舞い")

        result = _run_session_start_hook(
            temp_db,
            extra_env={"OW_ROLE": "worker"},
            stdin_payload={"session_id": "sess-stale-env"},
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "# アクティビティ一覧" in context
        assert "残存env下タスク" in context
        assert "# 振る舞い" in context
        assert "残存env下振る舞い" in context


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

    def test_invalid_db_signal_capture_failure_does_not_crash_hook(self):
        """シグナル捕捉自体が失敗する状況（DB到達不能）でもhookはクラッシュしない。

        try_capture_signal 経由の capture_signal_safe はDB接続不能を内部で握りつぶし、
        stderrにログを残した上でhookは空JSONを返し続ける（多層防御の内側の層が
        先に捕まえる想定通りの経路）。
        """
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
        assert json.loads(stdout) == {}
        assert "capture_signal_safe failed" in result.stderr


class TestSessionStartHookSyncPolicy:
    """sync_policyの注入テスト"""

    def test_sync_policy_shown_when_set(self, temp_db):
        """CALM_SYNC_POLICY設定時にsync_policyセクションが出力される"""
        result = _run_session_start_hook(
            temp_db, extra_env={"CALM_SYNC_POLICY": "PRマージ済みは自動で閉じて"}
        )
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "# sync_policy" in context
        assert "PRマージ済みは自動で閉じて" in context

    def test_sync_policy_hidden_when_unset(self, temp_db):
        """CALM_SYNC_POLICY未設定時にsync_policyセクションが出力されない"""
        result = _run_session_start_hook(
            temp_db, env_remove=["CALM_SYNC_POLICY"]
        )
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "# sync_policy" not in context

    def test_sync_policy_hidden_when_empty(self, temp_db):
        """CALM_SYNC_POLICY空文字時にsync_policyセクションが出力されない"""
        result = _run_session_start_hook(
            temp_db, extra_env={"CALM_SYNC_POLICY": ""}
        )
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "# sync_policy" not in context


class TestSessionStartHookTier2AndFixedNav:
    """階層3・4廃止後のダッシュボード（階層1・2の個別表示 + 末尾固定ナビ）のテスト"""

    def test_legacy_sections_removed(self, temp_db):
        """旧『直近作成(24h以内)』『スコアリング対象』のフラット2分割は出力されない"""
        _seed_activity("[作業] 普通の作業", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "## \U0001f195 直近作成（24h以内）" not in context
        assert "## スコアリング対象" not in context

    def test_stats_line_no_longer_emitted(self, temp_db):
        """旧統計行（他: ... → check_in・get_activitiesで確認）は出力されない"""
        _seed_activity("[作業] 優先タスク", status="in_progress")
        stale_id = _seed_activity("[作業] 古いpending", status="pending")
        old_iso = (datetime.now(timezone.utc) - timedelta(days=45)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        _set_updated_at(stale_id, old_iso)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "→ check_in・get_activitiesで確認" not in context
        assert "直近24h" not in context
        assert "30日以内" not in context

    def test_stale_non_pinned_pending_hidden_and_counted_in_nav(self, temp_db):
        """in_progressでもpinnedでもないpending（7日超はもちろん7日以内でも）
        は階層2に入らず、固定ナビの未表示件数句にのみ反映される"""
        excluded_id = _seed_activity("[作業] 古すぎタスク", status="pending")
        old_iso = (datetime.now(timezone.utc) - timedelta(days=45)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        _set_updated_at(excluded_id, old_iso)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert f"(#{excluded_id})" not in context
        assert "未表示のアクティビティ1件" in context

    def test_heartbeat_section_unaffected_by_tier3_4_removal(self, temp_db):
        """heartbeat (別セッション) セクションは階層3・4廃止の影響を受けない"""
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

        assert "## 作業中（別セッション）" in context
        heartbeat_idx = context.index("## 作業中（別セッション）")
        assert f"(#{activity_id})" in context[heartbeat_idx:]
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

    def test_fixed_nav_present_when_tier2_populated(self, temp_db):
        """階層2に表示があっても、固定ナビ（check_in導線）は末尾に出続ける"""
        _seed_activity("[作業] 優先タスク", status="in_progress")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "check_in（なければ作成 — activity-start）" in context


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


class TestSessionStartHookTier1And2:
    """階層1（作業中別セッション）・階層2（優先）の個別表示 + 末尾固定ナビのテスト"""

    def _seed_old_activity(
        self,
        title: str,
        status: str = "pending",
        days_ago: int = 3,
    ) -> int:
        """created_at / updated_at を N 日前に設定した activity を作成する"""
        activity_id = _seed_activity(title, status=status)
        iso = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        _set_created_at(activity_id, iso)
        _set_updated_at(activity_id, iso)
        return activity_id

    def test_tier_sections_in_expected_order(self, temp_db):
        """階層 1（別セッション）→ 階層 2（優先）→ 末尾固定文・固定ナビの順で出る"""
        heartbeat_id = _seed_activity("[作業] heartbeat別", status="in_progress")
        _set_heartbeat(heartbeat_id, session_id="sess-other")

        priority_id = _seed_activity("[作業] 優先タスク", status="in_progress")

        recent_id = _seed_activity("[作業] 新着タスク", status="pending")
        other_id = self._seed_old_activity("[作業] 過去タスク")

        result = _run_session_start_hook(
            temp_db, stdin_payload={"session_id": "sess-self"}
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        idx_tier1 = context.index("## 作業中（別セッション）")
        idx_tier2 = context.index("## 優先")
        idx_nav = context.index("check_in（なければ作成 — activity-start）")

        assert idx_tier1 < idx_tier2 < idx_nav
        assert f"(#{heartbeat_id})" in context[idx_tier1:idx_tier2]
        assert f"(#{priority_id})" in context[idx_tier2:idx_nav]
        # recent_id・other_id はいずれも in_progress でも pinned でもない
        # pending のため階層2に入らず、固定ナビの未表示件数句にのみ反映される
        assert f"(#{recent_id})" not in context
        assert f"(#{other_id})" not in context
        assert "未表示のアクティビティ2件" in context

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

    def test_meta_line_shown_only_when_blocked_by_present(self, temp_db):
        """階層 2 で blocked_by 未解決の依存があるときのみ meta 行が出る"""
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

    def test_pinned_overflow_appears_in_fixed_nav(self, temp_db):
        """pinned が階層 2 の上限(5)を溢れると、60日decay圏内でも階層2からは外れ、
        固定ナビの未表示件数句（pinned内訳）に計上される"""
        source_topic_id = _seed_topic("pin source topic")

        # 階層 2 の上限 5 件を埋める pinned（updated_at は新しめ）
        for i in range(5):
            filler_id = self._seed_old_activity(
                f"[作業] pinned filler {i}", status="pending", days_ago=2
            )
            _add_pin_activity("topic", source_topic_id, filler_id)

        # updated_at が最も古く（45日前、60日decay圏内）階層 2 上位 5 件から溢れる pinned
        stale_id = self._seed_old_activity(
            "[作業] 溢れたpinned", status="pending", days_ago=45
        )
        _add_pin_activity("topic", source_topic_id, stale_id)

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        # 階層 2『優先』には上位 5 件のみ入り、最古の pinned は溢れている
        idx_tier2 = context.index("## 優先")
        idx_next = context.find("\n## ", idx_tier2 + 1)
        tier2_block = (
            context[idx_tier2:] if idx_next == -1 else context[idx_tier2:idx_next]
        )
        assert f"(#{stale_id})" not in tier2_block, (
            "溢れた pinned が階層 2 の上限を無視して入っている"
        )

        # 溢れた pinned はどの階層にも個別出現せず、固定ナビのpinned内訳として
        # 脱落せずに残っている（脱落バグ回帰）
        assert f"(#{stale_id})" not in context
        assert "pinned 1件含む" in context

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


class TestSessionStartHookSignals:
    """未トリアージシグナルの1行表示テスト"""

    def test_no_signals_section_absent(self, temp_db):
        """新規シグナルが0件のときセクション自体が出ない"""
        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "未トリアージのシグナル" not in context

    def test_new_signal_shown_with_count(self, temp_db):
        """新規シグナルが1件以上あるとき件数付きで1行表示される"""
        _seed_signal("machine_error", "boom")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "未トリアージのシグナル: 1件 (machine_error 1) → get_signals で確認" in context

    def test_signal_breakdown_by_kind(self, temp_db):
        """複数kindのシグナルが件数内訳付きで表示される"""
        _seed_signal("machine_error", "boom 1")
        _seed_signal("machine_error", "boom 2")
        _seed_signal("friction", "使いにくい")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "未トリアージのシグナル: 3件" in context
        assert "machine_error 2" in context
        assert "friction 1" in context

    def test_triaged_signal_not_counted(self, temp_db):
        """status='new'以外のシグナルは件数に含まれない"""
        from src.services.signal_service import update_signal

        signal_id = _seed_signal("machine_error", "boom")
        update_signal(signal_id, status="dismissed")

        result = _run_session_start_hook(temp_db)
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "未トリアージのシグナル" not in context


class TestSessionStartHookTranscriptPath:
    """transcript_path 1行注入テスト（聞き返し検出tool一発化のための下地）"""

    def test_transcript_path_present_in_payload_is_injected(self, temp_db):
        """stdin payloadにtranscript_pathがあれば1行注入される"""
        result = _run_session_start_hook(
            temp_db, stdin_payload={"transcript_path": "/tmp/example-transcript.jsonl"}
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "このセッションのtranscript path: /tmp/example-transcript.jsonl" in context

    def test_transcript_path_absent_from_payload_no_injection(self, temp_db):
        """stdin payloadにtranscript_pathが無ければセクション自体が出ない"""
        result = _run_session_start_hook(temp_db, stdin_payload={"session_id": "sid-1"})
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "transcript path" not in context

    def test_transcript_path_non_string_ignored(self, temp_db):
        """transcript_pathが文字列でない場合は無視され注入されない"""
        result = _run_session_start_hook(temp_db, stdin_payload={"transcript_path": 12345})
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "transcript path" not in context


class TestSessionStartHookRelayInbox:
    """relay inbox未読件数 + Monitor監視指示の表示テスト

    CALM_RELAY_SESSION_AWARE=1（本クラスの既定extra_env）を渡した場合のON時の
    振る舞いを検証する。OFF時（未設定）の振る舞いはTestSessionStartHookRelay
    SessionAwareGateで検証する。

    hookは実プロセスとしてsubprocess経由で起動され、MCPリクエストコンテキストを
    一切持たない。そのためget_relay_identity()（ヘッダ/ctx.session_id経路）は
    常にNoneへ解決する。祖先pidチェーンによるフォールバック
    （resolve_identity_by_ancestry）は、hook subprocessの祖先チェーンと共通の
    祖先pidを持つ launcher 登録ファイルが実在する場合にのみ解決できる。
    以下のテストのうち登録ファイルを置かないケースは、この経路も解決できず
    従来通りゼロコストになることの確認であり、登録ファイルを置くケースは
    このテストプロセス自身のpidを共通祖先に見立てて実際に解決できることの
    確認である。
    """

    def test_no_relay_section_when_no_unread(self, temp_db, tmp_path):
        """relay状態が何もない通常時はセクション自体が出ない

        RELAY_STATE_DIR を空のtmp_pathへ隔離する（実行マシン上に本物の
        launcher登録ファイル・credential.jsonが存在すると、祖先pidチェーンが
        たまたま実launcherと共通祖先を持つケースでテストが非決定的になり得る
        ため）。
        """
        state_dir = tmp_path / "relay-state"
        result = _run_session_start_hook(
            temp_db,
            extra_env={
                "RELAY_STATE_DIR": str(state_dir),
                "CALM_RELAY_SESSION_AWARE": "1",
            },
            env_remove=["RELAY_BEARER_TOKEN"],
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "relay inbox 未読" not in context

    def test_relay_section_absent_without_matching_launcher_registration(
        self, temp_db, tmp_path
    ):
        """実在のinboxに未読が積まれていても、共通祖先を持つ launcher 登録
        ファイルが無ければidentityを解決できずセクションは表示されない。
        """
        state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(state_dir)
        try:
            relay_inbox.append("some-identity", {"body": "hello"})
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_session_start_hook(
            temp_db,
            extra_env={
                "RELAY_STATE_DIR": str(state_dir),
                "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
                "CALM_RELAY_SESSION_AWARE": "1",
            },
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "relay inbox 未読" not in context

    def _register_launcher_matching_this_test_process(self, state_dir) -> str:
        """このテストプロセス自身のpidを共通祖先に見立てた launcher 登録
        ファイルを作る。

        `_run_session_start_hook` は `subprocess.run` でhookを直接の子プロセス
        として起動するため、hook subprocess の ppid は必ずこのテストプロセスの
        pid（`os.getpid()`）になる。ここをancestor_pidsに含めておけば、hook側の
        resolve_identity_by_ancestry が実際に共通祖先を発見して解決できる。
        """
        session_id = "resolved-by-ancestry"
        sessions_dir = state_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        registration = {
            "session_id": session_id,
            "pid": os.getpid(),
            "ancestor_pids": [os.getpid()],
            "created_at": "2026-07-08T00:00:00Z",
        }
        (sessions_dir / f"launcher-{os.getpid()}.json").write_text(
            json.dumps(registration), encoding="utf-8"
        )
        return session_id

    def test_relay_section_shown_via_ancestry_fallback_when_registered(
        self, temp_db, tmp_path
    ):
        """共通祖先を持つ launcher 登録ファイルが実在する場合、hookは
        resolve_identity_by_ancestryでidentityを解決し、未読件数 + Monitor
        監視の指示を表示する。
        """
        state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(state_dir)
        try:
            session_id = self._register_launcher_matching_this_test_process(state_dir)
            relay_inbox.append(session_id, {"body": "hello"})
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_session_start_hook(
            temp_db,
            extra_env={
                "RELAY_STATE_DIR": str(state_dir),
                "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
                "CALM_RELAY_SESSION_AWARE": "1",
            },
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "relay inbox 未読: 1件" in context
        assert "Monitorツール" in context
        assert "relay_receive" in context

    def test_relay_section_absent_when_token_not_configured(self, temp_db, tmp_path):
        """identityは解決できてもrelay未構成（token未設定）ならセクションは出ない"""
        state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(state_dir)
        try:
            session_id = self._register_launcher_matching_this_test_process(state_dir)
            relay_inbox.append(session_id, {"body": "hello"})
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_session_start_hook(
            temp_db,
            extra_env={"RELAY_STATE_DIR": str(state_dir), "CALM_RELAY_SESSION_AWARE": "1"},
            env_remove=["RELAY_BEARER_TOKEN"],
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "relay inbox 未読" not in context

    def test_monitor_instruction_shown_when_inbox_never_created(self, temp_db, tmp_path):
        """identity解決・relay構成済みなら、このidentity宛のinbox fileが
        一度も作られていなくてもMonitor監視指示は出る（未読N件の報告行のみ省く。
        セッション作業中に届く新着を取りこぼさないための常時発火）。
        呼び出し後、inbox fileがtail -fの即時失敗を防ぐため先行生成されている"""
        state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(state_dir)
        try:
            session_id = self._register_launcher_matching_this_test_process(state_dir)
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_session_start_hook(
            temp_db,
            extra_env={
                "RELAY_STATE_DIR": str(state_dir),
                "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
                "CALM_RELAY_SESSION_AWARE": "1",
            },
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "Monitorツール" in context
        assert "relay inbox 未読" not in context
        os.environ["RELAY_STATE_DIR"] = str(state_dir)
        try:
            assert relay_inbox.inbox_path(session_id).exists()
        finally:
            del os.environ["RELAY_STATE_DIR"]

    def test_monitor_instruction_shown_when_unread_is_zero(self, temp_db, tmp_path):
        """identity解決・relay構成済みでinbox fileが存在し、既読化済みで
        未読が0件でもMonitor監視指示は出る（未読N件の報告行のみ省く）"""
        state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(state_dir)
        try:
            session_id = self._register_launcher_matching_this_test_process(state_dir)
            relay_inbox.append(session_id, {"body": "hello"})
            # peek=False（既定）で drain して既読化する
            relay_inbox.drain(session_id)
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_session_start_hook(
            temp_db,
            extra_env={
                "RELAY_STATE_DIR": str(state_dir),
                "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
                "CALM_RELAY_SESSION_AWARE": "1",
            },
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "Monitorツール" in context
        assert "relay inbox 未読" not in context


class TestSessionStartHookRelayInboxViaRealUv:
    """_CLI_HOP_WINDOW=2の前提を、実際の`uv run`経由でhookを起動して検証する。

    他のidentity解決テストは`_get_ppid`をモックするか`sys.executable`で直接
    pythonを起動しており、hooks.jsonが実際に使う`cd X && exec uv run python
    hooks/xxx.py`という起動経路そのものを検証していない。この経路が成立する
    のは「hook側もlauncher側もwrapper(uv)を1枚挟んでCLI本体から起動される」
    という構造に立脚しており、もし将来のuvが`uv run`内部でexecによる自己
    置換に切り替わると、祖先チェーンの段数が1つズレて窓内に端末ホスト
    （iTermServer・tmuxサーバ等）が入り込み、この判定が防いでいる誤クロス
    セッション一致が2ホップ窓の中でそのまま再発しうる（fail-closeにもならず
    静かに別セッションのidentityを返す、検知しづらい退行）。本テストは
    実際に`uv run`でhookを起動し、この前提が崩れたらCIで検知できるようにする。
    """

    def test_resolves_identity_through_real_uv_wrapper(self, temp_db, tmp_path):
        uv_path = shutil.which("uv")
        if uv_path is None:
            pytest.skip("uvがPATH上に無い環境のためスキップ")

        state_dir = tmp_path / "relay-state"
        sessions_dir = state_dir / "sessions"
        sessions_dir.mkdir(parents=True)

        # このテストプロセス自身をhookの起動元「Claude Code CLI本体」役に
        # 見立てる。hookをhooks.json同形の`exec uv run python hooks/xxx.py`
        # で実際に起動したとき、wrapper(uv)がこのプロセスの直接の子として
        # 生存し続け、hookプロセスはさらにその子になる
        # （hook -> uv -> このテストプロセス、ちょうど2ホップ）なら解決に
        # 成功するはず。
        session_id = "resolved-via-real-uv"
        registration = {
            "session_id": session_id,
            "pid": os.getpid(),
            "ancestor_pids": [os.getpid()],
            "created_at": "2026-07-08T00:00:00Z",
        }
        (sessions_dir / f"launcher-{os.getpid()}.json").write_text(
            json.dumps(registration), encoding="utf-8"
        )

        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(state_dir)
        try:
            relay_inbox.append(session_id, {"body": "hello"})
        finally:
            del os.environ["RELAY_STATE_DIR"]

        env = {
            **os.environ,
            "DISCUSSION_DB_PATH": temp_db,
            "RELAY_STATE_DIR": str(state_dir),
            "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
            "CALM_RELAY_SESSION_AWARE": "1",
        }
        env.pop("OW_ROLE", None)
        cleanup_dir = tempfile.mkdtemp(prefix="ccm-habits-rules-real-uv-")
        env["CALM_HABITS_RULES_PATH"] = str(Path(cleanup_dir) / "cc-memory-habits.md")

        try:
            # hooks.jsonと全く同じ形（`cd X && exec uv run python hooks/xxx.py`
            # をshに渡す）で起動する。execによりsh自身がuvへ置き換わるため、
            # hookプロセスの祖先チェーンにsh層が残らない。
            command = (
                f"cd {PROJECT_ROOT} && exec {uv_path} run python "
                "hooks/session_start_hook.py"
            )
            result = subprocess.run(
                ["sh", "-c", command],
                input="{}",
                capture_output=True,
                text=True,
                env=env,
            )
            stdout = result.stdout.strip()
            assert stdout, f"session_start_hook.py produced no output. stderr: {result.stderr}"
            output = json.loads(stdout)
        finally:
            shutil.rmtree(cleanup_dir, ignore_errors=True)

        context = output["hookSpecificOutput"]["additionalContext"]
        assert "relay inbox 未読: 1件" in context
        assert "Monitorツール" in context


class TestSessionStartHookRelaySessionAwareGate:
    """CALM_RELAY_SESSION_AWARE（kill switch）未設定時（デフォルトOFF）の振る舞い。

    token設定済み・launcher登録済みでidentity解決可能・未読ありという
    「本来なら表示される」全条件を満たしていても、env var未設定なら
    relay関連の文言が一切出ないことを検証する。
    """

    def test_no_relay_text_when_env_var_unset_even_if_fully_configured(
        self, temp_db, tmp_path
    ):
        state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(state_dir)
        try:
            sessions_dir = state_dir / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            session_id = "resolved-by-ancestry-gate-test"
            registration = {
                "session_id": session_id,
                "pid": os.getpid(),
                "ancestor_pids": [os.getpid()],
                "created_at": "2026-07-08T00:00:00Z",
            }
            (sessions_dir / f"launcher-{os.getpid()}.json").write_text(
                json.dumps(registration), encoding="utf-8"
            )
            relay_inbox.append(session_id, {"body": "hello"})
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_session_start_hook(
            temp_db,
            extra_env={
                "RELAY_STATE_DIR": str(state_dir),
                "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
            },
            env_remove=["CALM_RELAY_SESSION_AWARE"],
        )
        context = result["hookSpecificOutput"]["additionalContext"]

        assert "relay inbox" not in context
        assert "Monitorツール" not in context


class TestSessionStartHookContextBudget:
    """SessionStart additionalContext出力全体の字数回帰テスト

    構造部分（一覧・固定ナビ・末尾固定文等）の膨張を検知することが目的。
    本番相当のデータ量（habitsの大量投入等）による超過は別途の運用課題であり、
    ここでは標準的な小規模fixtureでの構造コストのみを検査する。
    """

    def test_standard_fixture_context_within_budget(self, temp_db):
        """habits数件・activity数件+pinned1件程度の標準fixtureで
        additionalContext合計が1,900字以下である"""
        for i in range(3):
            _seed_activity(f"[作業] 通常タスク{i}", status="in_progress")

        pinned_id = _seed_activity("[設計] pinned設計作業", status="pending")
        topic_id = _seed_topic("pin source")
        _add_pin_activity("topic", topic_id, pinned_id)

        for i in range(2):
            _seed_habit(f"標準的な長さの振る舞い内容その{i}")

        result = _run_session_start_hook(temp_db, env_remove=["CALM_SYNC_POLICY"])
        context = result["hookSpecificOutput"]["additionalContext"]

        assert len(context) <= 1900, (
            f"additionalContextが1,900字を超えている（実測{len(context)}字）"
        )
