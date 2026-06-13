"""iterm2.sh ターミナルアダプタのユニットテスト

osascriptをモックして、CWD/WORKER_CMD/TERM_REFに特殊文字が含まれても
AppleScriptインジェクションが発生しないことを確認する。
"""
import os
import subprocess
from pathlib import Path

ADAPTER = Path(__file__).resolve().parent.parent.parent / "scripts" / "ow" / "adapters" / "iterm2.sh"
MOCK_UUID = "mock-uuid-0000-1111-2222-333333333333"


def _make_mock_osascript(tmp_path: Path, capture_file: Path) -> Path:
    """osascriptをモックするシェルスクリプトを作成する。

    stdinで受け取ったAppleScriptをcapture_fileに追記し、モックUUIDを返す。
    """
    mock_dir = tmp_path / "mock_bin"
    mock_dir.mkdir(exist_ok=True)
    mock = mock_dir / "osascript"
    mock.write_text(f'#!/usr/bin/env bash\ncat >> "{capture_file}"\necho "{MOCK_UUID}"\n')
    mock.chmod(0o755)
    return mock_dir


def _run_adapter(args: list[str], tmp_path: Path) -> tuple[subprocess.CompletedProcess, str]:
    """iterm2.shをモックosascript環境で実行し、(result, captured_applescript)を返す。"""
    capture_file = tmp_path / "applescript_capture.txt"
    capture_file.write_text("")
    mock_dir = _make_mock_osascript(tmp_path, capture_file)

    env = os.environ.copy()
    env["PATH"] = str(mock_dir) + ":" + env["PATH"]

    result = subprocess.run(
        [str(ADAPTER)] + args,
        capture_output=True,
        text=True,
        env=env,
    )
    return result, capture_file.read_text()


class TestIterm2AdapterSpawn:
    def test_spawn_returns_session_uuid(self, tmp_path):
        """通常のCWD/WORKER_CMDでspawnが正常終了し、osascriptの出力を返す。"""
        result, _ = _run_adapter(["spawn", "/tmp/normal", "claude"], tmp_path)
        assert result.returncode == 0
        assert MOCK_UUID in result.stdout

    def test_spawn_cwd_with_spaces(self, tmp_path):
        """CWDにスペースが含まれてもspawnが正常終了し、AppleScriptに生の値が直接埋め込まれない。"""
        cwd = "/Users/John Doe/my project"
        result, captured = _run_adapter(["spawn", cwd, "claude"], tmp_path)
        assert result.returncode == 0
        assert MOCK_UUID in result.stdout
        assert cwd not in captured

    def test_spawn_worker_cmd_with_double_quotes(self, tmp_path):
        """WORKER_CMDにダブルクォートが含まれてもspawnが正常終了し、AppleScriptに生の値が直接埋め込まれない。"""
        cmd = 'claude --arg "value with spaces"'
        result, captured = _run_adapter(["spawn", "/tmp/work", cmd], tmp_path)
        assert result.returncode == 0
        assert MOCK_UUID in result.stdout
        assert cmd not in captured

    def test_spawn_cwd_with_single_quotes(self, tmp_path):
        """CWDにシングルクォートが含まれてもspawnが正常終了し、AppleScriptに生の値が直接埋め込まれない。"""
        cwd = "/Users/O'Brien/work"
        result, captured = _run_adapter(["spawn", cwd, "claude"], tmp_path)
        assert result.returncode == 0
        assert MOCK_UUID in result.stdout
        assert cwd not in captured

    def test_spawn_worker_cmd_with_backslash(self, tmp_path):
        """WORKER_CMDにバックスラッシュが含まれてもspawnが正常終了する。"""
        cmd = r'claude --path C:\Users\foo'
        result, _ = _run_adapter(["spawn", "/tmp/work", cmd], tmp_path)
        assert result.returncode == 0
        assert MOCK_UUID in result.stdout

    def test_spawn_applescript_contains_base64(self, tmp_path):
        """spawnで生成されたAppleScriptにbase64エンコード文字列が含まれる（インジェクション対策の確認）。"""
        import base64
        cwd = "/Users/John Doe/my project"
        expected_b64 = base64.b64encode(cwd.encode()).decode()
        _, captured = _run_adapter(["spawn", cwd, "claude"], tmp_path)
        assert expected_b64 in captured


class TestIterm2AdapterClose:
    def test_close_normal_uuid(self, tmp_path):
        """通常のUUID形式でcloseが正常終了する。"""
        result, _ = _run_adapter(
            ["close", "ABCD1234-5678-90EF-ABCD-EF0123456789"], tmp_path
        )
        assert result.returncode == 0

    def test_close_term_ref_with_double_quotes(self, tmp_path):
        """TERM_REFにダブルクォートが含まれてもcloseが正常終了し、AppleScriptに生の値が直接埋め込まれない。"""
        term_ref = 'uuid-with-"quotes"'
        result, captured = _run_adapter(["close", term_ref], tmp_path)
        assert result.returncode == 0
        assert term_ref not in captured

    def test_close_term_ref_with_spaces(self, tmp_path):
        """TERM_REFにスペースが含まれてもcloseが正常終了する。"""
        result, _ = _run_adapter(["close", "uuid with spaces"], tmp_path)
        assert result.returncode == 0

    def test_close_applescript_contains_base64(self, tmp_path):
        """closeで生成されたAppleScriptにTERM_REFのbase64エンコード文字列が含まれる。"""
        import base64
        term_ref = "ABCD1234-5678-90EF-ABCD-EF0123456789"
        expected_b64 = base64.b64encode(term_ref.encode()).decode()
        _, captured = _run_adapter(["close", term_ref], tmp_path)
        assert expected_b64 in captured


class TestIterm2AdapterErrors:
    def test_unknown_action_exits_nonzero(self, tmp_path):
        """未知のactionはゼロ以外のexit codeとエラーメッセージを返す。"""
        result, _ = _run_adapter(["unknown_action"], tmp_path)
        assert result.returncode != 0
        assert "Unknown action" in result.stderr
