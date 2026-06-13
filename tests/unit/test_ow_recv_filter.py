"""recv_filter.pyのユニットテスト

エッジケース:
- 自分宛（to=自分のhandle）→ stdout出力
- broadcast（to="*"）→ stdout出力
- 他者宛 → 出力しない
- 不正JSON行 → スキップ（クラッシュしない）
"""
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

RECV_FILTER = Path(__file__).resolve().parent.parent.parent / "scripts" / "ow" / "recv_filter.py"


def _make_sse_line(body_dict: dict) -> str:
    """SSEのdata行を作成する（bodyはJSON文字列化してmsgに格納）"""
    msg = {"msg_id": 1, "handle": "orch", "body": json.dumps(body_dict)}
    return f"data: {json.dumps(msg)}"


def _run_filter(handle: str, stdin_lines: list[str]) -> list[str]:
    """recv_filter.pyを実行してstdout行を返す"""
    result = subprocess.run(
        [sys.executable, str(RECV_FILTER), handle],
        input="\n".join(stdin_lines) + "\n",
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


class TestRecvFilterDirect:
    """recv_filter.pyのmain()をサブプロセスで直接テストする"""

    def test_outputs_message_addressed_to_me(self):
        """自分宛メッセージ（to=自分のhandle）はstdoutに出力される"""
        line = _make_sse_line({"v": 1, "kind": "cmd", "to": "orch", "from": "w-a"})
        output = _run_filter("orch", [line])
        assert len(output) == 1
        assert "data: " in output[0]

    def test_outputs_broadcast_message(self):
        """broadcast（to="*"）はstdoutに出力される"""
        line = _make_sse_line({"v": 1, "kind": "cmd", "to": "*", "from": "w-a"})
        output = _run_filter("orch", [line])
        assert len(output) == 1

    def test_skips_message_to_others(self):
        """他者宛メッセージはstdoutに出力されない"""
        line = _make_sse_line({"v": 1, "kind": "cmd", "to": "w-b", "from": "orch"})
        output = _run_filter("orch", [line])
        assert len(output) == 0

    def test_skips_invalid_json(self):
        """不正なJSON行はスキップ（クラッシュしない）"""
        lines = [
            "data: not-json-at-all",
            "data: {broken",
            _make_sse_line({"v": 1, "kind": "state", "to": "orch", "from": "w-a"}),
        ]
        output = _run_filter("orch", lines)
        # 有効な1件のみ出力される
        assert len(output) == 1

    def test_skips_non_data_lines(self):
        """SSEのkeep-alive等、data:で始まらない行はスキップ"""
        lines = [
            ": ping",
            "",
            "event: message",
            _make_sse_line({"v": 1, "kind": "state", "to": "orch", "from": "w-a"}),
        ]
        output = _run_filter("orch", lines)
        assert len(output) == 1

    def test_handles_multiple_messages(self):
        """複数行が混在する場合、自分宛のみ出力される"""
        lines = [
            _make_sse_line({"v": 1, "kind": "cmd", "to": "orch", "from": "w-a"}),  # 自分宛
            _make_sse_line({"v": 1, "kind": "state", "to": "w-b", "from": "orch"}),  # 他者宛
            _make_sse_line({"v": 1, "kind": "state", "to": "*", "from": "w-a"}),  # broadcast
        ]
        output = _run_filter("orch", lines)
        assert len(output) == 2

    def test_skips_message_with_no_to_field(self):
        """toフィールドがない場合はスキップ"""
        line = _make_sse_line({"v": 1, "kind": "cmd", "from": "w-a"})
        output = _run_filter("orch", [line])
        assert len(output) == 0


class TestRecvFilterModule:
    """recv_filter.pyのロジックをモジュールとして直接テストする"""

    def _call_filter(self, my_handle: str, lines: list[str]) -> list[str]:
        """sys.stdin/stdoutをモックしてmain()を実行する"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("recv_filter", RECV_FILTER)
        mod = importlib.util.module_from_spec(spec)

        captured_output: list[str] = []

        original_argv = sys.argv
        original_stdin = sys.stdin
        original_stdout = sys.stdout
        try:
            sys.argv = ["recv_filter.py", my_handle]
            sys.stdin = io.StringIO("\n".join(lines) + "\n")

            class CapturingWriter(io.StringIO):
                def write(self, s: str) -> int:
                    if s.strip():
                        captured_output.append(s.rstrip("\n"))
                    return len(s)

            sys.stdout = CapturingWriter()
            spec.loader.exec_module(mod)
            mod.main()
        finally:
            sys.argv = original_argv
            sys.stdin = original_stdin
            sys.stdout = original_stdout

        return captured_output

    def test_outputs_addressed_to_me(self):
        line = _make_sse_line({"v": 1, "kind": "cmd", "to": "orch", "from": "w-a"})
        output = self._call_filter("orch", [line])
        assert len(output) == 1

    def test_outputs_broadcast(self):
        line = _make_sse_line({"v": 1, "kind": "cmd", "to": "*", "from": "w-a"})
        output = self._call_filter("orch", [line])
        assert len(output) == 1

    def test_skips_others(self):
        line = _make_sse_line({"v": 1, "kind": "cmd", "to": "w-b", "from": "orch"})
        output = self._call_filter("orch", [line])
        assert len(output) == 0

    def test_skips_invalid_json(self):
        lines = ["data: {broken", "data: not-json"]
        output = self._call_filter("orch", lines)
        assert len(output) == 0
