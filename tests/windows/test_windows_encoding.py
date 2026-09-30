"""UTF-8モードを無効化した非UTF-8ロケール(Windows既定のANSIコードページ相当)で、
DB初期化とフック入出力が壊れないことを確かめる回帰テスト。

(a) init_database(R3): yoyoが未適用migrationファイルをencoding指定無しの
    open()で読むため、ロケールがcp932/cp1252等だとUnicodeDecodeErrorになる。
    サーバーはログ設定(logging.basicConfig)より前に落ちるため痕跡が残らない。

(b) hookの入出力(R7/R8): harness.read_hook_inputはテキストモードのsys.stdinを、
    _emitはensure_ascii=Falseのstdoutを使う。PYTHONIOENCODING=cp932を強制すると、
    日本語やU+2014を含むJSONの読み書きがOS問わず(Mac上でも)壊れうる。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.windows.support import REPO_ROOT, isolated_env, run_with_timeout

_INIT_DB_SCRIPT = "from src.db import init_database; init_database(); print('OK')"


def _forced_non_utf8_env(tmp_path: Path) -> dict:
    env = isolated_env(tmp_path)
    env["PYTHONIOENCODING"] = "cp932"
    env["PYTHONUTF8"] = "0"
    return env


def test_init_database_under_forced_non_utf8_locale(tmp_path):
    """R3: 新規DBに対するinit_database()が、UTF-8モード無効のロケールでも通ること。

    現行コードのままWindows(既定ANSIコードページがcp932/cp1252等)で実行すると、
    yoyoのmigrationファイル読み込みがUnicodeDecodeErrorになり、サーバーは
    起動前に落ちる。POSIX側はロケールがUTF-8である限りこの問題を再現しない
    (open()の既定encodingはPYTHONUTF8ではなくlocale.getpreferredencoding()に従う)。
    """
    env = _forced_non_utf8_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", _INIT_DB_SCRIPT],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "init_database() failed under a forced non-UTF-8 locale "
        f"(PYTHONUTF8=0, PYTHONIOENCODING=cp932):\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout


def _make_migrated_db(tmp_path: Path) -> Path:
    """cp932回帰の影響を受けない通常環境でDBを1回作っておき、hookテスト側は
    そのDBを使い回す(hook側のテストがR3の影響を受けて無関係に落ちるのを防ぐ)。
    """
    db_path = tmp_path / "prebuilt.db"
    env = isolated_env(tmp_path)
    env["CALM_DB_PATH"] = str(db_path)
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", _INIT_DB_SCRIPT],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"failed to prebuild a migrated DB for the hook encoding test:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    return db_path


def _run_hook_under_cp932(hook_relpath: str, payload: dict, tmp_path: Path, db_path: Path):
    env = _forced_non_utf8_env(tmp_path)
    env["CALM_DB_PATH"] = str(db_path)
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.touch()
    payload = {**payload, "transcript_path": str(transcript_path), "cwd": str(REPO_ROOT)}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return run_with_timeout(
        [sys.executable, hook_relpath],
        input_bytes=raw,
        cwd=REPO_ROOT,
        env=env,
        timeout=60,
    )


def _assert_hook_output_is_utf8_json(result, hook_name: str, error_marker: str) -> None:
    """終了コード・UTF-8としての妥当性に加えて、hook自身のfail-open診断ログ
    (`f"{hook_name} error: {{e}}"`をstderrへ書く、対象2フック共通の規約)が
    出ていないことも確認する。

    両フックとも`main()`全体をtry/exceptで囲み、UnicodeDecodeError /
    UnicodeEncodeErrorが起きても`harness.emit_empty()`で`{}`にfail-openする
    設計のため、「exit 0 かつ有効なJSON」だけでは常に真になってしまい
    (`{}`はUTF-8としてもJSONとしても常に妥当)、cp932下でR7/R8が実際に
    起きたかどうかを判別できない。fail-open時に必ず書かれるこの診断ログの
    有無で判別する。
    """
    assert result.returncode == 0, (
        f"{hook_name} exited with code={result.returncode} under a forced "
        f"cp932 stdio locale:\nstdout={result.stdout!r}\nstderr={result.stderr_text()}"
    )
    stdout_bytes = result.stdout.strip()
    if stdout_bytes:
        try:
            stdout_text = stdout_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            raise AssertionError(
                f"{hook_name} stdout is not valid UTF-8: {stdout_bytes!r} ({e})"
            )
        try:
            data = json.loads(stdout_text)
        except json.JSONDecodeError as e:
            raise AssertionError(
                f"{hook_name} stdout is not valid JSON once decoded as UTF-8: {stdout_text!r} ({e})"
            )
        assert isinstance(data, dict)

    stderr_text = result.stderr_text()
    assert error_marker not in stderr_text, (
        f"{hook_name} silently fell back to an empty response under cp932 stdio "
        f"(caught a Unicode error and degraded instead of handling it):\n{stderr_text}"
    )


def test_session_start_hook_survives_cp932_stdio(tmp_path):
    """R7/R8: SessionStartフックへ日本語+U+2014入りのJSONをcp932固定stdioで渡す。

    現行コードではharness.read_hook_input()がテキストモードのsys.stdinを読み、
    _emitがensure_ascii=Falseのstdoutへprintするため、cp932では入力の取りこぼし
    (R7)や出力側のUnicodeEncodeError(R8、hook自身の案内文に含まれるU+2014が
    cp932で符号化できない)が起きる。
    """
    db_path = _make_migrated_db(tmp_path)
    payload = {
        "session_id": "windows-repro-session",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "_test_marker": "日本語のテスト文字列 — em dash を含む",
    }
    result = _run_hook_under_cp932("hooks/session_start_hook.py", payload, tmp_path, db_path)
    _assert_hook_output_is_utf8_json(result, "session_start_hook.py", "session_start_hook.py error")


def test_user_prompt_submit_hook_survives_cp932_stdio(tmp_path):
    """R7/R8: UserPromptSubmitフックへ日本語+U+2014入りのpromptをcp932固定stdioで渡す。"""
    db_path = _make_migrated_db(tmp_path)
    payload = {
        "session_id": "windows-repro-session",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "日本語のプロンプトです — テスト用の入力",
    }
    result = _run_hook_under_cp932("hooks/user_prompt_submit_hook.py", payload, tmp_path, db_path)
    _assert_hook_output_is_utf8_json(
        result, "user_prompt_submit_hook.py", "user_prompt_submit_hook.py error"
    )
