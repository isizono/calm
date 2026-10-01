"""UTF-8モードを無効化した非UTF-8ロケール(Windows既定のANSIコードページ相当)で、
DB初期化とフック入出力が壊れないことを確かめる回帰テスト。修正前のコードが
どう壊れるか(=この修正が無いと何が起きるか)の説明であり、現行コードの挙動
の説明ではない。

(a) init_database: yoyoが未適用migrationファイルをencoding指定無しの
    open()で読むため、ロケールがcp932/cp1252等だとUnicodeDecodeErrorになる。
    サードパーティのyoyo自身は直せないため、対策は.mcp.jsonのcalm.env
    (PYTHONUTF8=1)でlauncher起動時にUTF-8モードを強制することだけである。

(b) hookの入出力: 修正前はharness.read_hook_inputがテキストモードの
    sys.stdinを、_emitがensure_ascii=Falseのstdoutを使っていた。
    PYTHONIOENCODING=cp932を強制すると、日本語やU+2014を含むJSONの
    読み書きがOS問わず(Mac上でも)壊れうる。現行コードはbuffer経由の
    UTF-8読み取りとensure_ascii=Trueで、ロケールに依存しない。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.windows.support import REPO_ROOT, isolated_env, load_mcp_launcher_command, run_with_timeout

_INIT_DB_SCRIPT = "from src.db import init_database; init_database(); print('OK')"


def _forced_non_utf8_env(tmp_path: Path) -> dict:
    env = isolated_env(tmp_path)
    env["PYTHONIOENCODING"] = "cp932"
    env["PYTHONUTF8"] = "0"
    return env


def _real_non_utf8_locale_available() -> bool:
    """POSIXでja_JP.SJISロケールが使えるか(無ければinit_databaseテストをskipする)。"""
    if sys.platform == "win32":
        return True
    try:
        result = subprocess.run(["locale", "-a"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "ja_JP.SJIS" in result.stdout.split()


def _non_utf8_locale_env(tmp_path: Path) -> dict:
    """open()の既定encodingまで実際に非UTF-8(cp932相当)にする環境を返す。

    PYTHONIOENCODINGはsys.stdin/stdout/stderrのエンコーディングにしか効かず、
    open()の既定encoding(locale.getpreferredencoding(False)が決める)には影響
    しない。WindowsはANSIコードページ自体が既定で非UTF-8なのでPYTHONUTF8=0
    だけで足りるが、POSIXではLC_ALL/LANGでロケールそのものを切り替える必要が
    ある(_real_non_utf8_locale_availableがFalseの環境では呼び出し側がskipする)。
    """
    env = isolated_env(tmp_path)
    env["PYTHONUTF8"] = "0"
    if sys.platform == "win32":
        env["PYTHONIOENCODING"] = "cp932"
    else:
        env.pop("PYTHONIOENCODING", None)
        env["LC_ALL"] = "ja_JP.SJIS"
        env["LANG"] = "ja_JP.SJIS"
    return env


def test_init_database_under_forced_non_utf8_locale(tmp_path):
    """新規DBに対するinit_database()が、UTF-8モード無効のロケールでも通ること。

    現行コードのままWindows(既定ANSIコードページがcp932/cp1252等)で実行すると、
    yoyoのmigrationファイル読み込みがUnicodeDecodeErrorになり、サーバーは
    起動前に落ちる。この問題への対策は.mcp.jsonのcalm.env(PYTHONUTF8=1)で
    launcher経由の起動時にUTF-8モードを強制することだけであり、yoyo自身の
    open()はサードパーティのコードで直接は直せない。そのため、このテストは
    ロケールそのものを非UTF-8にした上から.mcp.jsonのenvを重ねる
    (本番の入口=launcher起動と同じ条件を再現し、.mcp.jsonのenvが消える・
    書き換わる退行を拾う)。
    """
    if not _real_non_utf8_locale_available():
        pytest.skip("ja_JP.SJIS locale not available on this system")
    env = _non_utf8_locale_env(tmp_path)
    _, mcp_env_overrides = load_mcp_launcher_command()
    env.update(mcp_env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", _INIT_DB_SCRIPT],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        # text=Trueだと、このpytestプロセス自身のロケール(Windows CIランナー上
        # ではcp1252)でstdout/stderrバイト列をデコードする。cp1252はいくつかの
        # バイト値(0x81/0x8D/0x8F/0x90/0x9D)が未定義で、UTF-8/cp932の日本語の
        # マルチバイト列と衝突しうる。この修正自体の検証がデコード例外で無関係に
        # 落ちないよう、encodingを明示する。
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, (
        "init_database() failed under a forced non-UTF-8 locale layered with "
        ".mcp.json's calm.env (the production launcher entry point):\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout


def test_mcp_json_forces_python_utf8():
    """init_database()を非UTF-8ロケールから守る唯一の対策である.mcp.jsonの
    calm.env(PYTHONUTF8=1)が消えていないことを直接確認する。

    test_init_database_under_forced_non_utf8_localeはja_JP.SJISロケールが
    無い環境ではskipされるため、そうした環境でもこの配線自体の退行だけは
    拾えるよう、ロケール非依存の最小限の保険として置く。
    """
    _, mcp_env_overrides = load_mcp_launcher_command()
    assert mcp_env_overrides.get("PYTHONUTF8") == "1"


def test_snapshot_cli_list_survives_cp932_stdio(tmp_path):
    """R19: backup_service CLI（scripts/snapshot.py）が非UTF-8ロケールでも
    日本語メッセージを正しく出力すること。

    main()先頭のsys.stdout.reconfigure(encoding="utf-8")を外すと、本テストは
    「スナップショットはありません」のバイト列がcp932表現のまま出力され、
    UTF-8としてデコードした結果が一致しなくなる（確認済み）。
    """
    env = _forced_non_utf8_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "scripts/snapshot.py", "list"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"scripts/snapshot.py list failed under a forced cp932 stdio locale:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "スナップショットはありません" in result.stdout.decode("utf-8")


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
