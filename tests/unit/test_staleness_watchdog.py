"""staleness_watchdogモジュールのユニットテスト"""
import shutil
import threading
import time

from src.infra.staleness_watchdog import (
    CHECK_INTERVAL_ENV,
    DEBOUNCE_ENV,
    DEFAULT_CHECK_INTERVAL_SEC,
    DEFAULT_DEBOUNCE_SEC,
    StalenessWatchdog,
    _read_check_interval_sec,
    _read_debounce_sec,
)


class TestNoChangeNoShutdown:
    def test_no_change_never_triggers_shutdown(self, tmp_path):
        """起動時ハッシュ計算 → ファイル内容変更なし → shutdown callbackが呼ばれない"""
        (tmp_path / "a.py").write_text("original")

        shutdown_called = threading.Event()
        wd = StalenessWatchdog(
            tmp_path, check_interval_sec=0.2, debounce_sec=0.1,
            shutdown_callback=shutdown_called.set,
        )
        wd.start()
        try:
            # 複数チェックサイクル分待っても発火しないこと
            assert shutdown_called.wait(timeout=1) is False
        finally:
            wd.stop()


class TestChangeDetectedShutdown:
    def test_sustained_change_triggers_shutdown_after_debounce(self, tmp_path):
        """ファイル内容変更 → 1回目のチェックで疑い → debounce後も変化が残っている → shutdown callback発火"""
        target = tmp_path / "a.py"
        target.write_text("original")

        shutdown_called = threading.Event()
        wd = StalenessWatchdog(
            tmp_path, check_interval_sec=0.1, debounce_sec=0.1,
            shutdown_callback=shutdown_called.set,
        )
        wd.start()
        try:
            target.write_text("changed")
            assert shutdown_called.wait(timeout=3) is True
        finally:
            wd.stop()


class TestRollingBugRegression:
    def test_shutdown_fires_even_after_multiple_check_cycles_elapse(self, tmp_path):
        """ローリングバグの回帰テスト

        比較は常に起動時ベースラインに対して行われる契約を検証する。
        「前回計算したハッシュ」を都度更新して次回と比較するローリング実装
        だと、1回目のチェックで偽の「変化なし」判定に落ち着き、以降
        永久に検知できなくなる。ファイル内容を変更した状態のまま複数回
        （2回以上）のチェックサイクルを実際に経過させても shutdown
        callback が確実に発火することを、_compute_hash の呼び出し回数
        (spy) で複数サイクル経過を確認しつつ検証する。
        """
        target = tmp_path / "a.py"
        target.write_text("original")

        shutdown_called = threading.Event()
        wd = StalenessWatchdog(
            tmp_path, check_interval_sec=0.05, debounce_sec=0.05,
            shutdown_callback=shutdown_called.set,
        )

        call_count = {"n": 0}
        original_compute = wd._compute_hash

        def counting_compute():
            call_count["n"] += 1
            return original_compute()

        wd._compute_hash = counting_compute
        wd.start()
        try:
            target.write_text("changed")
            assert shutdown_called.wait(timeout=3) is True
            # baseline計算(1) + 最低1チェックサイクル(current)+debounce再確認(recheck) で
            # 最低3回。複数サイクル分の余裕を持って2回以上を必須条件とする。
            assert call_count["n"] >= 2
        finally:
            wd.stop()


class TestShutdownIsOneShot:
    def test_shutdown_callback_fires_only_once_and_loop_stops(self, tmp_path):
        """陳腐化確定後はループが自ら停止し、shutdown callbackが複数回呼ばれない

        stop()を明示的に呼ばずに放置した場合、次のcheck_interval経過後も
        コードが元に戻っていなければ同じベースライン比較で再び確定に達し、
        shutdown_callback（実プロダクションではSIGINT送信）を繰り返し
        呼んでしまう回帰を防ぐ。短いcheck_interval運用で最初のgraceful
        shutdownが完了しきる前に2回目のシャットダウン試行が割り込む
        リスクへの回帰ガード。
        """
        target = tmp_path / "a.py"
        target.write_text("original")

        call_count = {"n": 0}
        first_call = threading.Event()

        def on_shutdown():
            call_count["n"] += 1
            first_call.set()

        wd = StalenessWatchdog(
            tmp_path, check_interval_sec=0.05, debounce_sec=0.05,
            shutdown_callback=on_shutdown,
        )
        wd.start()
        try:
            target.write_text("changed")
            assert first_call.wait(timeout=3) is True
            # stop()を呼ばずに複数チェックサイクル分待つ。ループが自律停止
            # していなければ、この間に再度shutdown_callbackが呼ばれるはず。
            time.sleep(1)
            assert call_count["n"] == 1
            assert wd._stop_event.is_set() is True
        finally:
            wd.stop()


class TestDebounceRevertNoShutdown:
    def test_reverted_before_recheck_does_not_trigger_shutdown(self, tmp_path):
        """debounce recheckの前に元の内容へ戻す(=偽陽性) → shutdown callbackが呼ばれない"""
        target = tmp_path / "a.py"
        target.write_text("original")

        shutdown_called = threading.Event()
        # ループスレッド自体は長い間隔で実質眠らせ、_check_once()を直接同期呼び出しする
        wd = StalenessWatchdog(
            tmp_path, check_interval_sec=999, debounce_sec=0.2,
            shutdown_callback=shutdown_called.set,
        )
        wd.start()
        try:
            target.write_text("changed")

            def revert_during_debounce():
                time.sleep(0.05)
                target.write_text("original")

            reverter = threading.Thread(target=revert_during_debounce, daemon=True)
            reverter.start()

            wd._check_once()  # debounce待機を含め同期的に1回分実行
            reverter.join()

            assert shutdown_called.is_set() is False
        finally:
            wd.stop()


class TestExcludedDirs:
    EXCLUDED_DIR_NAMES = [
        "__pycache__", ".venv", ".git", ".pytest_cache", ".in_use", ".trees",
    ]

    def test_excluded_dir_names_do_not_affect_hash_at_any_depth(self, tmp_path):
        """除外対象ディレクトリ名配下のファイルはどの深さでもハッシュに影響しない"""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("app")

        wd = StalenessWatchdog(tmp_path)
        baseline = wd._compute_hash()

        for name in self.EXCLUDED_DIR_NAMES:
            # ネストした深さに置いても刈られることを確認する
            d = tmp_path / "src" / "nested" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "junk.bin").write_text("junk")

        assert wd._compute_hash() == baseline

    def test_claude_worktrees_relative_path_excluded(self, tmp_path):
        """`.claude/worktrees` は相対パス指定でピンポイントに除外される"""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("app")

        wd = StalenessWatchdog(tmp_path)
        baseline = wd._compute_hash()

        wt_dir = tmp_path / ".claude" / "worktrees" / "agent-x"
        wt_dir.mkdir(parents=True)
        (wt_dir / "junk.txt").write_text("junk")

        assert wd._compute_hash() == baseline


class TestNonExcludedLookalikePaths:
    def test_claude_agents_changes_are_detected(self, tmp_path):
        """`.claude` 自体は刈られず、`.claude/agents/` の変更は検知される"""
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "builder.md").write_text("v1")

        wd = StalenessWatchdog(tmp_path)
        baseline = wd._compute_hash()

        (agents_dir / "builder.md").write_text("v2")

        assert wd._compute_hash() != baseline

    def test_claude_skills_changes_are_detected(self, tmp_path):
        """`.claude/skills/` の変更も検知される（`.claude`丸ごと除外の誤実装ガード）"""
        skills_dir = tmp_path / ".claude" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("v1")

        wd = StalenessWatchdog(tmp_path)
        baseline = wd._compute_hash()

        (skills_dir / "SKILL.md").write_text("v2")

        assert wd._compute_hash() != baseline


class TestProjectRootMissing:
    def test_check_once_skips_without_exception_when_root_deleted(self, tmp_path):
        """project_root自体が削除されていても例外を投げず正常にスキップされる"""
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.py").write_text("x")

        shutdown_called = threading.Event()
        wd = StalenessWatchdog(
            root, check_interval_sec=999, debounce_sec=0.05,
            shutdown_callback=shutdown_called.set,
        )
        wd.start()
        try:
            shutil.rmtree(root)
            wd._check_once()  # raiseしないこと
            assert shutdown_called.is_set() is False
        finally:
            wd.stop()


class TestCheckIntervalZeroDisablesThread:
    def test_check_interval_zero_disables_watchdog_thread(self, tmp_path):
        """check_interval_sec=0の場合、スレッドが起動しない"""
        wd = StalenessWatchdog(tmp_path, check_interval_sec=0)
        wd.start()
        assert wd._thread is None

    def test_check_interval_zero_skips_baseline_hash_computation(self, tmp_path):
        """check_interval_sec=0の場合、無効化判定が_compute_hash()より先に行われ、

        プロジェクトルート全体を読み切る重い処理を無効化ケースでも
        同期実行してしまうことがない（結果が使われないbaseline計算の無駄打ち
        の回帰ガード）。
        """
        (tmp_path / "a.py").write_text("x")

        wd = StalenessWatchdog(tmp_path, check_interval_sec=0)

        call_count = {"n": 0}
        original_compute = wd._compute_hash

        def counting_compute():
            call_count["n"] += 1
            return original_compute()

        wd._compute_hash = counting_compute
        wd.start()

        assert call_count["n"] == 0
        assert wd._baseline_hash is None


class TestReadCheckIntervalSec:
    """env CALM_STALENESS_CHECK_INTERVAL_SEC を読み取るヘルパーの単体テスト"""

    def test_env_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv(CHECK_INTERVAL_ENV, raising=False)
        assert _read_check_interval_sec() == DEFAULT_CHECK_INTERVAL_SEC

    def test_env_empty_returns_default(self, monkeypatch):
        monkeypatch.setenv(CHECK_INTERVAL_ENV, "")
        assert _read_check_interval_sec() == DEFAULT_CHECK_INTERVAL_SEC

    def test_env_numeric_returns_value(self, monkeypatch):
        monkeypatch.setenv(CHECK_INTERVAL_ENV, "120")
        assert _read_check_interval_sec() == 120

    def test_env_zero_returns_zero(self, monkeypatch):
        """env=0 は0をそのまま返す（watchdog無効化マーカー）"""
        monkeypatch.setenv(CHECK_INTERVAL_ENV, "0")
        assert _read_check_interval_sec() == 0

    def test_env_invalid_returns_default(self, monkeypatch, capsys):
        monkeypatch.setenv(CHECK_INTERVAL_ENV, "abc")
        assert _read_check_interval_sec() == DEFAULT_CHECK_INTERVAL_SEC
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "Invalid" in captured.err

    def test_env_negative_returns_default(self, monkeypatch, capsys):
        monkeypatch.setenv(CHECK_INTERVAL_ENV, "-1")
        assert _read_check_interval_sec() == DEFAULT_CHECK_INTERVAL_SEC
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert ">= 0" in captured.err


class TestReadDebounceSec:
    """env CALM_STALENESS_DEBOUNCE_SEC を読み取るヘルパーの単体テスト"""

    def test_env_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv(DEBOUNCE_ENV, raising=False)
        assert _read_debounce_sec() == DEFAULT_DEBOUNCE_SEC

    def test_env_numeric_returns_value(self, monkeypatch):
        monkeypatch.setenv(DEBOUNCE_ENV, "45")
        assert _read_debounce_sec() == 45

    def test_env_invalid_returns_default(self, monkeypatch, capsys):
        monkeypatch.setenv(DEBOUNCE_ENV, "abc")
        assert _read_debounce_sec() == DEFAULT_DEBOUNCE_SEC
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "Invalid" in captured.err

    def test_env_negative_returns_default(self, monkeypatch, capsys):
        monkeypatch.setenv(DEBOUNCE_ENV, "-1")
        assert _read_debounce_sec() == DEFAULT_DEBOUNCE_SEC
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert ">= 0" in captured.err


class TestConstructorEnvOverride:
    """StalenessWatchdog コンストラクタの env override 挙動"""

    def test_default_uses_env_value(self, monkeypatch, tmp_path):
        monkeypatch.setenv(CHECK_INTERVAL_ENV, "120")
        wd = StalenessWatchdog(tmp_path)
        assert wd._check_interval == 120

    def test_explicit_arg_overrides_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv(CHECK_INTERVAL_ENV, "120")
        wd = StalenessWatchdog(tmp_path, check_interval_sec=5)
        assert wd._check_interval == 5

    def test_explicit_zero_overrides_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv(CHECK_INTERVAL_ENV, "120")
        wd = StalenessWatchdog(tmp_path, check_interval_sec=0)
        assert wd._check_interval == 0
