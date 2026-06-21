"""recv_filter.pyのユニットテスト

エッジケース:
- 自分宛（to=自分のhandle）→ stdout出力
- broadcast（to="*"）→ stdout出力
- 他者宛 → 出力しない
- 不正JSON行 → スキップ（クラッシュしない）
- envelope schema (v / kind / data.type) を欠く → drop
- OW_FILTER_TASK opt-in: task 不一致 → drop
"""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RECV_FILTER = Path(__file__).resolve().parent.parent.parent / "scripts" / "ow" / "recv_filter.py"


def _make_sse_line(body_dict: dict) -> str:
    """SSEのdata行を作成する（bodyはJSON文字列化してmsgに格納）"""
    msg = {"msg_id": 1, "handle": "orch", "body": json.dumps(body_dict)}
    return f"data: {json.dumps(msg)}"


def _valid_envelope(**overrides) -> dict:
    """envelope schema を満たす最小 body を生成する (v=1 / kind=event / data.type=state)。"""
    base = {"v": 1, "kind": "event", "to": "orch", "from": "w-a", "data": {"type": "state"}}
    base.update(overrides)
    return base


def _run_filter(handle: str, stdin_lines: list[str], env: dict | None = None) -> list[str]:
    """recv_filter.pyを実行してstdout行を返す"""
    run_env = {**os.environ}
    run_env.pop("OW_FILTER_TASK", None)
    if env:
        run_env.update(env)
    result = subprocess.run(
        [sys.executable, str(RECV_FILTER), handle],
        input="\n".join(stdin_lines) + "\n",
        capture_output=True,
        text=True,
        env=run_env,
    )
    return [line for line in result.stdout.splitlines() if line]


class TestRecvFilterDirect:
    """recv_filter.pyのmain()をサブプロセスで直接テストする"""

    def test_outputs_message_addressed_to_me(self):
        """自分宛メッセージ（to=自分のhandle）はstdoutに出力される"""
        line = _make_sse_line(_valid_envelope(kind="command", to="orch", data={"type": "assign"}))
        output = _run_filter("orch", [line])
        assert len(output) == 1
        assert "data: " in output[0]

    def test_outputs_broadcast_message(self):
        """broadcast（to="*"）はstdoutに出力される"""
        line = _make_sse_line(_valid_envelope(to="*", data={"type": "heartbeat"}))
        output = _run_filter("orch", [line])
        assert len(output) == 1

    def test_skips_message_to_others(self):
        """他者宛メッセージはstdoutに出力されない"""
        line = _make_sse_line(_valid_envelope(to="w-b"))
        output = _run_filter("orch", [line])
        assert len(output) == 0

    def test_skips_invalid_json(self):
        """不正なJSON行はスキップ（クラッシュしない）"""
        lines = [
            "data: not-json-at-all",
            "data: {broken",
            _make_sse_line(_valid_envelope()),
        ]
        output = _run_filter("orch", lines)
        assert len(output) == 1

    def test_skips_non_data_lines(self):
        """SSEのkeep-alive等、data:で始まらない行はスキップ"""
        lines = [
            ": ping",
            "",
            "event: message",
            _make_sse_line(_valid_envelope()),
        ]
        output = _run_filter("orch", lines)
        assert len(output) == 1

    def test_handles_multiple_messages(self):
        """複数行が混在する場合、自分宛のみ出力される"""
        lines = [
            _make_sse_line(_valid_envelope(kind="command", to="orch", data={"type": "assign"})),
            _make_sse_line(_valid_envelope(to="w-b")),
            _make_sse_line(_valid_envelope(to="*", data={"type": "heartbeat"})),
        ]
        output = _run_filter("orch", lines)
        assert len(output) == 2

    def test_skips_message_with_no_to_field(self):
        """toフィールドがない場合はスキップ"""
        body = _valid_envelope()
        body.pop("to")
        line = _make_sse_line(body)
        output = _run_filter("orch", [line])
        assert len(output) == 0


class TestRecvFilterSchemaValidation:
    """envelope schema 検証 (v / kind / data.type)。"""

    def test_drops_envelope_without_v(self):
        body = _valid_envelope()
        body.pop("v")
        output = _run_filter("orch", [_make_sse_line(body)])
        assert len(output) == 0

    def test_drops_envelope_with_wrong_v(self):
        output = _run_filter("orch", [_make_sse_line(_valid_envelope(v=2))])
        assert len(output) == 0

    def test_drops_envelope_with_old_kind_state(self):
        """kind: 'state' (旧形式) は drop。"""
        output = _run_filter("orch", [_make_sse_line(_valid_envelope(kind="state"))])
        assert len(output) == 0

    def test_drops_envelope_with_old_kind_cmd(self):
        """kind: 'cmd' (略記) は drop ('command' 必須)。"""
        output = _run_filter("orch", [_make_sse_line(_valid_envelope(kind="cmd"))])
        assert len(output) == 0

    def test_drops_envelope_without_data_type(self):
        output = _run_filter("orch", [_make_sse_line(_valid_envelope(data={}))])
        assert len(output) == 0

    def test_drops_envelope_without_data(self):
        body = _valid_envelope()
        body.pop("data")
        output = _run_filter("orch", [_make_sse_line(body)])
        assert len(output) == 0

    def test_drops_envelope_with_non_dict_data(self):
        output = _run_filter("orch", [_make_sse_line(_valid_envelope(data="not-a-dict"))])
        assert len(output) == 0

    def test_accepts_valid_v1_command(self):
        body = _valid_envelope(kind="command", data={"type": "assign"})
        output = _run_filter("orch", [_make_sse_line(body)])
        assert len(output) == 1

    def test_accepts_valid_v1_event(self):
        body = _valid_envelope(kind="event", to="*", data={"type": "heartbeat"})
        output = _run_filter("orch", [_make_sse_line(body)])
        assert len(output) == 1


class TestRecvFilterTaskFilter:
    """OW_FILTER_TASK opt-in による task filter。"""

    def test_keeps_matching_task(self):
        body = _valid_envelope(task="T119")
        output = _run_filter("orch", [_make_sse_line(body)], env={"OW_FILTER_TASK": "T119"})
        assert len(output) == 1

    def test_drops_non_matching_task(self):
        body = _valid_envelope(task="T200")
        output = _run_filter("orch", [_make_sse_line(body)], env={"OW_FILTER_TASK": "T119"})
        assert len(output) == 0

    def test_drops_other_task_broadcast(self):
        """他 task の broadcast event (heartbeat 等) を drop。"""
        body = _valid_envelope(to="*", task="T200", data={"type": "heartbeat"})
        output = _run_filter("orch", [_make_sse_line(body)], env={"OW_FILTER_TASK": "T119"})
        assert len(output) == 0

    def test_drops_envelope_without_task_when_filter_set(self):
        """task フィールド未設定の envelope も OW_FILTER_TASK 指定時は drop。"""
        body = _valid_envelope()
        body.pop("task", None)
        output = _run_filter("orch", [_make_sse_line(body)], env={"OW_FILTER_TASK": "T119"})
        assert len(output) == 0

    def test_no_filter_when_env_unset(self):
        """OW_FILTER_TASK 未設定なら task 検証なし (従来挙動)。"""
        body = _valid_envelope(to="*", task="T200", data={"type": "heartbeat"})
        output = _run_filter("orch", [_make_sse_line(body)])
        assert len(output) == 1

    def test_empty_string_filter_is_inactive(self):
        """OW_FILTER_TASK が空文字列の場合は未指定と同等扱い。"""
        body = _valid_envelope(to="*", task="T200", data={"type": "heartbeat"})
        output = _run_filter("orch", [_make_sse_line(body)], env={"OW_FILTER_TASK": ""})
        assert len(output) == 1


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
        body = _valid_envelope(kind="command", to="orch", data={"type": "assign"})
        output = self._call_filter("orch", [_make_sse_line(body)])
        assert len(output) == 1

    def test_outputs_broadcast(self):
        body = _valid_envelope(to="*", data={"type": "heartbeat"})
        output = self._call_filter("orch", [_make_sse_line(body)])
        assert len(output) == 1

    def test_skips_others(self):
        body = _valid_envelope(to="w-b")
        output = self._call_filter("orch", [_make_sse_line(body)])
        assert len(output) == 0

    def test_skips_invalid_json(self):
        lines = ["data: {broken", "data: not-json"]
        output = self._call_filter("orch", lines)
        assert len(output) == 0
