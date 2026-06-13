"""queueファイルのパース/シリアライズのユニットテスト（M#219 §3.2）"""
import json
from pathlib import Path

import pytest

from src.services import ow_service

SAMPLE_QUEUE_CONTENT = """\
---
topic_id: 454
channel_code: AbCdEfGh
---

## T1 | pin引き継ぎUI実装 | in_progress
- worker: w-a / term_ref: iterm2:8CDF1801-ABCD / session: sess-1
- activity: 801

## T2 | データ移行スクリプト作成 | queued
"""


class TestParseQueueFile:
    def test_parses_basic_queue(self, tmp_path: Path):
        """基本的なqueueファイルをパースできる"""
        queue_file = tmp_path / "queue-t454.md"
        queue_file.write_text(SAMPLE_QUEUE_CONTENT, encoding="utf-8")
        tasks = ow_service._parse_queue_file(queue_file)
        assert len(tasks) == 2

    def test_parses_task_fields(self, tmp_path: Path):
        """タスクのフィールドが正しくパースされる"""
        queue_file = tmp_path / "queue-t454.md"
        queue_file.write_text(SAMPLE_QUEUE_CONTENT, encoding="utf-8")
        tasks = ow_service._parse_queue_file(queue_file)
        t1 = tasks[0]
        assert t1["task"] == "T1"
        assert t1["title"] == "pin引き継ぎUI実装"
        assert t1["status"] == "in_progress"
        assert t1["term_ref"] == "iterm2:8CDF1801-ABCD"

    def test_parses_queued_task(self, tmp_path: Path):
        """queuedステータスのタスクもパースできる"""
        queue_file = tmp_path / "queue-t454.md"
        queue_file.write_text(SAMPLE_QUEUE_CONTENT, encoding="utf-8")
        tasks = ow_service._parse_queue_file(queue_file)
        t2 = tasks[1]
        assert t2["task"] == "T2"
        assert t2["status"] == "queued"

    def test_returns_empty_for_nonexistent_file(self, tmp_path: Path):
        """存在しないファイルは空リストを返す"""
        queue_file = tmp_path / "queue-t999.md"
        tasks = ow_service._parse_queue_file(queue_file)
        assert tasks == []

    def test_returns_empty_for_empty_file(self, tmp_path: Path):
        """空のファイルは空リストを返す"""
        queue_file = tmp_path / "queue-t0.md"
        queue_file.write_text("", encoding="utf-8")
        tasks = ow_service._parse_queue_file(queue_file)
        assert tasks == []

    def test_handles_pipe_in_title(self, tmp_path: Path):
        """タイトルに ' | ' が含まれていてもstatusが正しくパースされる"""
        content = "## T1 | fix: A | B問題 | in_progress\n- worker: w-a / term_ref: uuid-1 / session: s1\n"
        queue_file = tmp_path / "queue-t1.md"
        queue_file.write_text(content, encoding="utf-8")
        tasks = ow_service._parse_queue_file(queue_file)
        assert len(tasks) == 1
        assert tasks[0]["title"] == "fix: A | B問題"
        assert tasks[0]["status"] == "in_progress"

    def test_handles_spawning_status(self, tmp_path: Path):
        """spawningステータスのwrite-aheadエントリをパースできる"""
        content = "## T3 | new task | spawning\n- worker: w-b / term_ref: (pending) / session: (pending)\n"
        queue_file = tmp_path / "queue-t1.md"
        queue_file.write_text(content, encoding="utf-8")
        tasks = ow_service._parse_queue_file(queue_file)
        assert len(tasks) == 1
        assert tasks[0]["status"] == "spawning"


class TestWriteQueueSpawning:
    def test_creates_queue_file(self, tmp_path: Path):
        """queueファイルにspawning write-aheadエントリが作成される"""
        ow_service._write_queue_spawning(tmp_path, "454", "w-a", 1, "/tmp")
        queue_file = tmp_path / "queue-t454.md"
        assert queue_file.exists()
        content = queue_file.read_text(encoding="utf-8")
        assert "spawning" in content
        assert "T1" in content

    def test_appends_to_existing_file(self, tmp_path: Path):
        """既存のqueueファイルにspawning write-aheadエントリを追記する"""
        queue_file = tmp_path / "queue-t100.md"
        queue_file.write_text("## T1 | 既存タスク | done\n")
        ow_service._write_queue_spawning(tmp_path, "100", "w-b", 2, "/tmp")
        content = queue_file.read_text(encoding="utf-8")
        assert "既存タスク" in content
        assert "T2" in content


class TestOwSpawnWorkerManualFallback:
    """ケース#14: アダプタ不在時にmanualフォールバックで起動コマンドを返す"""

    def test_manual_fallback_when_ow_terminal_unset(self, tmp_path: Path, monkeypatch):
        """OW_TERMINAL未設定 → manual=True + commandキーを返す"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.delenv("OW_TERMINAL", raising=False)
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)

        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="ch1", cwd="/tmp", model="sonnet",
            task_title="test", acceptance="done", topic_id="99", task_n=1,
        )
        assert result.get("manual") is True
        assert "command" in result
        assert "OW_ROLE=worker" in result["command"]
        assert "task_file" in result

    def test_manual_fallback_when_ow_terminal_manual(self, tmp_path: Path, monkeypatch):
        """OW_TERMINAL=manual → manual=True"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "manual")
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)

        result = ow_service.ow_spawn_worker(
            alias="w-b", channel="ch2", cwd="/tmp", model="haiku",
            task_title="manual", acceptance="ok", task_n=2,
        )
        assert result.get("manual") is True
        assert "command" in result


class TestOwStatusIntegration:
    """ケース#16: queueパース + /presence合成 → 統合ビューを返す"""

    def test_status_merges_queue_and_presence(self, tmp_path: Path, monkeypatch):
        """queue内のworkerとpresenceの突合でonlineフラグが付く"""
        queue_content = (
            "## T1 | タスクA | in_progress\n"
            "- worker: w-a / term_ref: uuid-1 / session: s1\n\n"
            "## T2 | タスクB | queued\n"
        )
        queue_file = tmp_path / "queue-t100.md"
        queue_file.write_text(queue_content, encoding="utf-8")

        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))

        def fake_relay_request(method, path, data=None):
            if "/presence" in path:
                return {"handles": ["orch", "w-a"]}
            return {"error": "unexpected"}

        monkeypatch.setattr(ow_service, "_relay_request", fake_relay_request)

        result = ow_service.ow_status(channel="ch", topic_id="100")

        assert len(result["tasks"]) == 2
        assert result["tasks"][0]["online"] is True
        assert result["presence"] == ["orch", "w-a"]
        assert result["summary"]["total_tasks"] == 2
        assert "w-a" in result["summary"]["online_workers"]

    def test_status_with_empty_queue(self, tmp_path: Path, monkeypatch):
        """queueファイルなし + presence取得 → タスク0件の統合ビュー"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))

        def fake_relay_request(method, path, data=None):
            return {"handles": ["orch"]}

        monkeypatch.setattr(ow_service, "_relay_request", fake_relay_request)

        result = ow_service.ow_status(channel="ch", topic_id="999")
        assert result["tasks"] == []
        assert result["summary"]["total_tasks"] == 0


class TestOwCloseWorkerTermRef:
    """ケース#17: term_ref(安定ID)で対象を特定"""

    def test_close_passes_term_ref_to_adapter(self, tmp_path: Path, monkeypatch):
        """アダプタにterm_refが正しく渡される"""
        adapter_script = tmp_path / "adapters" / "iterm2.sh"
        adapter_script.parent.mkdir(parents=True, exist_ok=True)
        adapter_script.write_text("#!/bin/bash\nexit 0\n")
        adapter_script.chmod(0o755)

        monkeypatch.setenv("OW_TERMINAL", "iterm2")
        monkeypatch.setattr(ow_service, "_get_adapter_path", lambda t: adapter_script)

        captured_args = []
        original_run = ow_service.subprocess.run

        def fake_run(args, **kwargs):
            captured_args.extend(args)
            from unittest.mock import MagicMock
            return MagicMock(returncode=0)

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        result = ow_service.ow_close_worker(term_ref="8CDF1801-ABCD-1234")

        assert result.get("closed") is True
        assert "8CDF1801-ABCD-1234" in captured_args
        assert result["term_ref"] == "8CDF1801-ABCD-1234"

    def test_close_manual_fallback(self, monkeypatch):
        """アダプタ不在時はmanualフォールバック"""
        monkeypatch.delenv("OW_TERMINAL", raising=False)

        result = ow_service.ow_close_worker(term_ref="some-uuid")
        assert result.get("manual") is True
        assert "some-uuid" in result.get("message", "")


class TestWriteTaskFile:
    def test_creates_task_json(self, tmp_path: Path):
        """task fileがJSONとして作成される"""
        task_file = ow_service._write_task_file(
            task_dir=tmp_path, task_n=1, alias="w-a", channel="AbCdEfGh",
            cwd="/tmp", model="sonnet", permission="acceptEdits",
            task_title="Test", acceptance="pass", context="ctx",
            playbook="", timeout_min=60, activity_id=1, topic_id="10"
        )
        assert task_file.exists()
        data = json.loads(task_file.read_text(encoding="utf-8"))
        assert data["task"] == "T1"
        assert data["alias"] == "w-a"
        assert data["v"] == 1

    def test_task_file_name(self, tmp_path: Path):
        """task fileの名前がT{n}.json形式になる"""
        task_file = ow_service._write_task_file(
            task_dir=tmp_path, task_n=5, alias="w-e", channel="ch1",
            cwd="/tmp", model="haiku", permission="default",
            task_title="", acceptance="", context="", playbook="",
            timeout_min=30, activity_id=None, topic_id=None
        )
        assert task_file.name == "T5.json"

    def test_creates_parent_dirs(self, tmp_path: Path):
        """親ディレクトリが存在しなくても作成する"""
        nested_dir = tmp_path / "deep" / "nested" / "tasks"
        task_file = ow_service._write_task_file(
            task_dir=nested_dir, task_n=1, alias="w-a", channel="ch",
            cwd="/tmp", model="sonnet", permission="acceptEdits",
            task_title="", acceptance="", context="", playbook="",
            timeout_min=60, activity_id=None, topic_id=None
        )
        assert task_file.exists()
