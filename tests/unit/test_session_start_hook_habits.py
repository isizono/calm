"""hooks/session_start_hook.py の habits セクション組み立てロジックの直接ユニットテスト

tests/e2e/test_session_start_hook.py はsubprocess経由でhookを呼ぶため、
habit_projection.verify_and_healの戻り値をプロセス境界を越えてモックできない
（モジュール docstring 参照）。本ファイルは hooks.session_start_hook を
in-process import し、verify_and_heal を monkeypatch することで、
failed_stale/failed_absent の分岐（バグ修正確認）・body再利用によるDB二重
クエリの回避（効率性修正確認）・SessionStart(source=compact) 時の強制再注入
（マージ前ゲート対応確認）を、実DB往復なしで決定論的に検証する。
"""
from hooks import session_start_hook
from src.db import get_connection
from src.services import habit_projection
from src.services.habit_service import add_habit


def _add_always(content: str) -> int:
    """ゲートを経由せず、直接trigger_mode='always'の振る舞いを作る
    （tests/unit/test_habit_projection.py の同名ヘルパーと同じ用途。
    add_habitの既定trigger_modeは'intelligently'のため、always層の全文検証には
    明示的な切り替えが必要）。"""
    habit_id = add_habit(content)["habit_id"]
    conn = get_connection()
    try:
        conn.execute("UPDATE habits SET trigger_mode = 'always' WHERE id = ?", (habit_id,))
        conn.commit()
    finally:
        conn.close()
    return habit_id


class TestBuildHabitsSectionStatusBranches:
    """verify_and_healの各statusに対するhookの通知文言の分岐テスト"""

    def test_fresh_yields_empty(self, temp_db, monkeypatch):
        monkeypatch.setattr(
            habit_projection,
            "verify_and_heal",
            lambda conn: {"status": "fresh", "body": "x", "always_contents": [], "manifest": []},
        )
        conn = get_connection()
        try:
            result = session_start_hook._build_habits_section(conn)
        finally:
            conn.close()

        assert result == ""

    def test_healed_stale_yields_single_line_success_notice(self, temp_db, monkeypatch):
        monkeypatch.setattr(
            habit_projection,
            "verify_and_heal",
            lambda conn: {
                "status": "healed_stale",
                "body": "x",
                "always_contents": ["healed_stale時のalways内容"],
                "manifest": [],
            },
        )
        conn = get_connection()
        try:
            result = session_start_hook._build_habits_section(conn)
        finally:
            conn.close()

        assert result == session_start_hook._HABITS_STALE_NOTICE
        assert "最新化した" in result
        # healed_stale（修復成功）は1行通知のみで、全文フォールバックは注入しない
        assert "healed_stale時のalways内容" not in result

    def test_failed_stale_does_not_reuse_success_notice(self, temp_db, monkeypatch):
        """バグ修正確認: failed_stale（修復失敗）はhealed_staleと同じ
        「最新化した」成功通知を返してはならない"""
        monkeypatch.setattr(
            habit_projection,
            "verify_and_heal",
            lambda conn: {
                "status": "failed_stale",
                "body": "x",
                "always_contents": ["failed_stale時のalways全文"],
                "manifest": [],
            },
        )
        conn = get_connection()
        try:
            result = session_start_hook._build_habits_section(conn)
        finally:
            conn.close()

        assert result != session_start_hook._HABITS_STALE_NOTICE
        assert "最新化した" not in result

    def test_failed_stale_yields_failure_notice_and_full_fallback(self, temp_db, monkeypatch):
        """バグ修正確認: failed_staleは修復失敗が分かる文言を返し、かつ absent系
        同様にalways層全文フォールバックを注入する（安全側のフォールバック）"""
        monkeypatch.setattr(
            habit_projection,
            "verify_and_heal",
            lambda conn: {
                "status": "failed_stale",
                "body": "x",
                "always_contents": ["failed_stale時のalways全文"],
                "manifest": [],
            },
        )
        conn = get_connection()
        try:
            result = session_start_hook._build_habits_section(conn)
        finally:
            conn.close()

        assert "失敗" in result
        assert "failed_stale時のalways全文" in result

    def test_failed_absent_yields_full_fallback(self, temp_db, monkeypatch):
        monkeypatch.setattr(
            habit_projection,
            "verify_and_heal",
            lambda conn: {
                "status": "failed_absent",
                "body": "x",
                "always_contents": ["failed_absent時のalways全文"],
                "manifest": [],
            },
        )
        conn = get_connection()
        try:
            result = session_start_hook._build_habits_section(conn)
        finally:
            conn.close()

        assert "failed_absent時のalways全文" in result

    def test_healed_absent_yields_full_fallback(self, temp_db, monkeypatch):
        monkeypatch.setattr(
            habit_projection,
            "verify_and_heal",
            lambda conn: {
                "status": "healed_absent",
                "body": "x",
                "always_contents": ["healed_absent時のalways全文"],
                "manifest": [],
            },
        )
        conn = get_connection()
        try:
            result = session_start_hook._build_habits_section(conn)
        finally:
            conn.close()

        assert "healed_absent時のalways全文" in result


class TestBuildHabitsSectionBodyReuse:
    """効率性修正確認: verify_and_healが返したalways_contents/manifestの再利用テスト"""

    def test_fallback_reuses_verify_and_heal_layers_without_requerying_db(self, temp_db, monkeypatch):
        """verify_and_healが既に取得したalways_contents/manifestを
        _build_degraded_habits_fallbackが再利用し、
        get_active_habit_contents_with_connへ再クエリしないこと"""
        add_habit("DB実データ用habit（再利用時は使われないはず）")

        monkeypatch.setattr(
            habit_projection,
            "verify_and_heal",
            lambda conn: {
                "status": "failed_absent",
                "body": "x",
                "always_contents": ["再利用された内容"],
                "manifest": [],
            },
        )

        def _explode(*args, **kwargs):
            raise AssertionError(
                "get_active_habit_contents_with_conn should not be called when "
                "verify_and_heal already supplied always_contents/manifest"
            )

        monkeypatch.setattr(session_start_hook, "get_active_habit_contents_with_conn", _explode)
        monkeypatch.setattr(session_start_hook, "list_intelligently_habit_manifest_with_conn", _explode)

        conn = get_connection()
        try:
            result = session_start_hook._build_habits_section(conn)
        finally:
            conn.close()

        assert "再利用された内容" in result
        assert "DB実データ用habit" not in result

    def test_disabled_status_still_queries_db_since_layers_are_none(self, temp_db, monkeypatch):
        """disabled(kill switch)はverify_and_healがDBに触れずalways_contents/
        manifestがNoneで返るため、_build_degraded_habits_fallbackは自前で
        クエリして実データを注入する（フォールバック自体は従来通り機能する）"""
        _add_always("kill switch下で実際に注入されるべきhabit")

        monkeypatch.setattr(
            habit_projection,
            "verify_and_heal",
            lambda conn: {"status": "disabled", "body": "", "always_contents": None, "manifest": None},
        )

        conn = get_connection()
        try:
            result = session_start_hook._build_habits_section(conn)
        finally:
            conn.close()

        assert "kill switch下で実際に注入されるべきhabit" in result


class TestBuildHabitsSectionCompactSource:
    """マージ前ゲート対応確認: SessionStart(source=compact) 時の強制再注入テスト"""

    def test_compact_source_forces_fallback_even_when_fresh(self, temp_db, monkeypatch):
        """compact後にrulesファイル内容がコンテキストへ保持されるかは実機未検証
        のため、source='compact'時はfresh判定でも鮮度に関わらず全文
        フォールバックを注入する（安全側に倒す仕様）"""
        monkeypatch.setattr(
            habit_projection,
            "verify_and_heal",
            lambda conn: {
                "status": "fresh",
                "body": "x",
                "always_contents": ["compact時に再注入されるべき内容"],
                "manifest": [],
            },
        )
        conn = get_connection()
        try:
            result = session_start_hook._build_habits_section(conn, source="compact")
        finally:
            conn.close()

        assert "compact時に再注入されるべき内容" in result

    def test_non_compact_source_keeps_fresh_empty(self, temp_db, monkeypatch):
        """source未指定・compact以外では従来通りfresh時は空文字のまま"""
        monkeypatch.setattr(
            habit_projection,
            "verify_and_heal",
            lambda conn: {"status": "fresh", "body": "x", "always_contents": [], "manifest": []},
        )
        conn = get_connection()
        try:
            result_default = session_start_hook._build_habits_section(conn)
            result_startup = session_start_hook._build_habits_section(conn, source="startup")
        finally:
            conn.close()

        assert result_default == ""
        assert result_startup == ""
