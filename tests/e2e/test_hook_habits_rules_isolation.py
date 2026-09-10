"""hookをsubprocess起動するテストが実ファイル（~/.claude/rules/cc-memory-habits.md）
へ誤って書き込まないための隔離メカニズムの回帰テスト。

conftestのautouse fixture（_isolate_habits_rules_projection）はテストプロセス内の
`config.HABITS_RULES_PATH` をpatchするだけで、hookは別プロセスで起動するため
isolationが伝播しない。tests.helpers.session_start_hook_env はこれを補うために
subprocess env へ CALM_HABITS_RULES_PATH を常に明示注入する。

tests/e2e/test_snapshot.py・tests/e2e/test_session_start_hook.py はいずれも
tests.helpers.run_session_start_hook / run_session_start_hook_process /
session_start_hook_env 経由でしか hooks/session_start_hook.py をsubprocess
起動しない。この隔離契約さえここで保証されていれば、新規テストがこれらの
ヘルパーを使う限り実ファイルへの書き込みは構造的に発生しない。
"""
from pathlib import Path

import pytest

from tests.helpers import (
    REAL_HABITS_RULES_PATH,
    run_session_start_hook,
    session_start_hook_env,
)


def test_session_start_hook_env_rejects_real_habits_path():
    """habits_rules_pathへ実ファイルパスを明示指定すると、env組み立て時点で拒否される。

    書き忘れ（未指定）だけでなく、コピペ等で実ファイルパスを誤って明示指定した
    ケースも同じ経路で弾く。
    """
    with pytest.raises(ValueError, match="実ファイルパス"):
        with session_start_hook_env("dummy-db-path", habits_rules_path=REAL_HABITS_RULES_PATH):
            pass  # pragma: no cover - raises before yield


def test_session_start_hook_env_defaults_to_isolated_tmp_path():
    """habits_rules_path未指定時、CALM_HABITS_RULES_PATHは実ファイルパスと異なる
    使い捨てパスへ自動的に差し替わる。with block を抜けると使い捨てディレクトリは
    削除される。
    """
    with session_start_hook_env("dummy-db-path") as env:
        injected_path = env["CALM_HABITS_RULES_PATH"]
        assert injected_path != REAL_HABITS_RULES_PATH
        assert env["DISCUSSION_DB_PATH"] == "dummy-db-path"
        tmp_dir = Path(injected_path).parent
        assert tmp_dir.is_dir()

    # with block を抜けた後は使い捨てディレクトリが削除されている
    assert not tmp_dir.exists()


def test_run_session_start_hook_writes_habits_projection_to_injected_path_only(
    temp_db, tmp_path
):
    """hookを実際にsubprocess起動したとき、habits投影ファイルの書き込み先が
    注入したCALM_HABITS_RULES_PATHへ実際に向くこと。

    verify_and_healは投影ファイル不在時に必ず新規作成する（absent→healed_absent）。
    habits_rules_pathへ明示指定した隔離パスにファイルが実際に作られることをもって、
    hookの書き込みが実ファイルではなく注入したパスへ向いたことを確認する
    （config.HABITS_RULES_PATHの値がhookプロセス内の書き込み先を一意に決めるため、
    ここへの書き込みが確認できれば実ファイルへは書き込まれていない）。
    """
    isolated_path = tmp_path / "cc-memory-habits.md"
    assert not isolated_path.exists()

    result = run_session_start_hook(temp_db, habits_rules_path=str(isolated_path))

    assert "hookSpecificOutput" in result
    assert isolated_path.is_file(), "habits投影ファイルが注入した隔離パスへ書き込まれていない"
    written = isolated_path.read_text(encoding="utf-8")
    assert "振る舞い" in written
