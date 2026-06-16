"""ow crash復旧自動化＋spawn前バリデーションのユニットテスト (T17)

カバー範囲:
- `_validate_spawn_preconditions`: relay/channel/cwd/alias 各失敗ケース + 全クリア
- `reconstruct_state_from_relay`: 単一 worker / 複数 worker / 重複 state / 非state混在
- `detect_crash_inconsistencies`: ghost_active / stalled_done / orphans 各分類
- `_apply_queue_status_update`: header status 置換 / note 追加・上書き / 不在 task
- `ow_recover`: dry_run / 自動修正適用 / relay不可

突合ロジック本体は純粋関数のためHTTPはモックに差し替えず直接呼ぶ。
relay HTTP接点（ow_history / _get_presence / ow_send 等）はmonkeypatchで差し替える。
"""
import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.services import ow_service


# ----------------------------
# _validate_spawn_preconditions
# ----------------------------


class TestValidateSpawnPreconditions:
    """spawn前ヘルスチェックの各検証項目を独立して検証する。"""

    @pytest.fixture(autouse=True)
    def _allow_relay_channel(self, monkeypatch):
        """relay/channelは既定でOKを返す。各テストで個別に差し替える。"""
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: True)
        monkeypatch.setattr(ow_service, "_get_presence", lambda ch: [])
        # デフォルトはidentity未存在（alive identityなし）
        monkeypatch.setattr(ow_service, "ow_get_identity", lambda ch, h: None)

    def test_all_ok_returns_no_warnings(self, monkeypatch, tmp_path):
        """全項目クリア → ok=True、warnings空"""
        result = ow_service._validate_spawn_preconditions(
            alias="w-x", channel="ChAbCdEf", cwd=str(tmp_path)
        )
        assert result["ok"] is True
        assert result["warnings"] == []

    def test_relay_unreachable_short_circuits(self, monkeypatch, tmp_path):
        """relay到達不可 → 即座にok=Falseで返り、それ以降のチェックは行わない"""
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: False)
        # ensure_channelは呼ばれないことを検証
        channel_calls = []
        monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: channel_calls.append(ch) or True)

        result = ow_service._validate_spawn_preconditions(
            alias="w-x", channel="ChAbCdEf", cwd=str(tmp_path)
        )
        assert result["ok"] is False
        assert any("relay" in w for w in result["warnings"])
        assert channel_calls == []

    def test_channel_unavailable_is_warning(self, monkeypatch, tmp_path):
        """channel作成失敗 → ok=False、warningにchannel名"""
        monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: False)
        result = ow_service._validate_spawn_preconditions(
            alias="w-x", channel="ChAbCdEf", cwd=str(tmp_path)
        )
        assert result["ok"] is False
        assert any("channel ChAbCdEf" in w for w in result["warnings"])

    def test_cwd_missing_is_warning(self, monkeypatch, tmp_path):
        """存在しないcwd → ok=False、warningにcwdパス"""
        missing = tmp_path / "does-not-exist"
        result = ow_service._validate_spawn_preconditions(
            alias="w-x", channel="ChAbCdEf", cwd=str(missing)
        )
        assert result["ok"] is False
        assert any("cwd" in w and str(missing) in w for w in result["warnings"])

    def test_cwd_is_file_not_dir(self, monkeypatch, tmp_path):
        """cwdがファイル → is_dir失敗で警告"""
        f = tmp_path / "afile"
        f.write_text("x")
        result = ow_service._validate_spawn_preconditions(
            alias="w-x", channel="ChAbCdEf", cwd=str(f)
        )
        assert result["ok"] is False
        assert any("cwd" in w for w in result["warnings"])

    def test_alias_in_presence_is_warning(self, monkeypatch, tmp_path):
        """presenceに既にaliasがいる → ok=False"""
        monkeypatch.setattr(ow_service, "_get_presence", lambda ch: ["orch", "w-x"])
        result = ow_service._validate_spawn_preconditions(
            alias="w-x", channel="ChAbCdEf", cwd=str(tmp_path)
        )
        assert result["ok"] is False
        assert any("alias w-x" in w and "online" in w for w in result["warnings"])

    def test_alias_in_active_queue_task_is_warning(self, monkeypatch, tmp_path):
        """queueで活動中のタスクに同一aliasが他task_nで割当て済み → ok=False"""
        # queue設定: w-x が T9 で working 中
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        queue_file = queue_dir / "queue-t454.md"
        queue_file.write_text(
            "## T9 | dummy | working\n"
            "- worker: w-x / term_ref: x / session: y\n"
        )
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))

        # 異なるtask_n (T10) で再spawn試行 → 警告
        result = ow_service._validate_spawn_preconditions(
            alias="w-x",
            channel="ChAbCdEf",
            cwd=str(tmp_path),
            topic_id="454",
            task_n=10,
        )
        assert result["ok"] is False
        assert any("active queue task T9" in w for w in result["warnings"])

    def test_same_task_n_respawn_is_allowed(self, monkeypatch, tmp_path):
        """同一task_nでの再spawn = 再リンクとみなして許可（warning出ない）"""
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        queue_file = queue_dir / "queue-t454.md"
        queue_file.write_text(
            "## T9 | dummy | working\n"
            "- worker: w-x / term_ref: x / session: y\n"
        )
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))

        result = ow_service._validate_spawn_preconditions(
            alias="w-x",
            channel="ChAbCdEf",
            cwd=str(tmp_path),
            topic_id="454",
            task_n=9,
        )
        assert result["ok"] is True
        assert result["warnings"] == []

    def test_alias_alive_identity_blocks_spawn(self, monkeypatch, tmp_path):
        """alive identity（terminated_at/cause未設定）があればok=False・INV-9警告"""
        monkeypatch.setattr(
            ow_service,
            "ow_get_identity",
            lambda ch, h: {
                "type": "identity",
                "role": "worker",
                "handle": h,
                "alias": h,
                # terminated_at / cause がない = alive
            },
        )
        result = ow_service._validate_spawn_preconditions(
            alias="w-x", channel="ChAbCdEf", cwd=str(tmp_path)
        )
        assert result["ok"] is False
        assert any("INV-9" in w for w in result["warnings"])

    def test_no_identity_allows_spawn(self, monkeypatch, tmp_path):
        """identityが存在しない（ow_get_identity=None）→ ok=True"""
        # autouseフィクスチャがNoneを返すデフォルト設定
        result = ow_service._validate_spawn_preconditions(
            alias="w-x", channel="ChAbCdEf", cwd=str(tmp_path)
        )
        assert result["ok"] is True
        assert result["warnings"] == []

    def test_terminated_identity_allows_spawn(self, monkeypatch, tmp_path):
        """terminatedなidentity（cause=closed / terminated_at設定）→ ok=True"""
        for terminated_identity in [
            {"handle": "w-x", "cause": "closed", "terminated_at": "2026-06-16T00:00:00Z"},
            {"handle": "w-x", "cause": "cancelled", "terminated_at": "2026-06-16T00:00:00Z"},
            {"handle": "w-x", "cause": "dead", "terminated_at": "2026-06-16T00:00:00Z"},
            {"handle": "w-x", "inferred_cause": "crashed (inferred)"},
        ]:
            monkeypatch.setattr(
                ow_service,
                "ow_get_identity",
                lambda ch, h, _ident=terminated_identity: _ident,
            )
            result = ow_service._validate_spawn_preconditions(
                alias="w-x", channel="ChAbCdEf", cwd=str(tmp_path)
            )
            assert result["ok"] is True, f"terminated identity should allow spawn: {terminated_identity}"
            assert result["warnings"] == [], f"unexpected warning for {terminated_identity}"


# ----------------------------
# reconstruct_state_from_relay
# ----------------------------


class TestReconstructStateFromRelay:
    """relay履歴から最新state宣言を集計する純粋ロジック。"""

    def _build_history(self, messages: list[dict]) -> dict:
        """ow_history相当の戻り値を組み立てる。"""
        return {"messages": messages}

    def test_empty_history(self, monkeypatch):
        """履歴空 → by_worker_taskが空・max_msg_id=0"""
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: self._build_history([]),
        )
        result = ow_service.reconstruct_state_from_relay("ChAbCdEf")
        assert result == {"by_worker_task": {}, "max_msg_id": 0, "truncated": False}

    def test_single_worker_multiple_states(self, monkeypatch):
        """同一workerが ready→working→done と進む → latest_state=done"""
        msgs = [
            {"msg_id": 1, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "T1", "data": {"type": "state", "state": "ready"}}, "created_at": "t1"},
            {"msg_id": 2, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "T1", "data": {"type": "state", "state": "working"}}, "created_at": "t2"},
            {"msg_id": 3, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "T1", "data": {"type": "state", "state": "done"}}, "created_at": "t3"},
        ]
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: self._build_history(msgs),
        )
        result = ow_service.reconstruct_state_from_relay("ChAbCdEf")
        assert result["max_msg_id"] == 3
        entry = result["by_worker_task"]["w-a:T1"]
        assert entry["latest_state"] == "done"
        assert entry["latest_msg_id"] == 3
        assert entry["latest_at"] == "t3"
        assert entry["history_count"] == 3

    def test_multiple_workers_isolated(self, monkeypatch):
        """異なる (alias, task) のstateは独立に集計される"""
        msgs = [
            {"msg_id": 1, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "T1", "data": {"type": "state", "state": "ready"}}, "created_at": "t1"},
            {"msg_id": 2, "body": {"v": 1, "kind": "event", "from": "w-b", "task": "T2", "data": {"type": "state", "state": "working"}}, "created_at": "t2"},
        ]
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: self._build_history(msgs),
        )
        result = ow_service.reconstruct_state_from_relay("ChAbCdEf")
        assert set(result["by_worker_task"]) == {"w-a:T1", "w-b:T2"}
        assert result["by_worker_task"]["w-a:T1"]["latest_state"] == "ready"
        assert result["by_worker_task"]["w-b:T2"]["latest_state"] == "working"

    def test_command_messages_are_ignored(self, monkeypatch):
        """kind=commandのメッセージはstate集計から除外される"""
        msgs = [
            {"msg_id": 1, "body": {"v": 1, "kind": "command", "from": "orch", "to": "w-a", "task": "T1", "data": {"type": "assign"}}},
            {"msg_id": 2, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "T1", "data": {"type": "state", "state": "ready"}}, "created_at": "t2"},
        ]
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: self._build_history(msgs),
        )
        result = ow_service.reconstruct_state_from_relay("ChAbCdEf")
        assert "w-a:T1" in result["by_worker_task"]
        # ready 1件のみ集計、commandはhistory_countに含まれない
        assert result["by_worker_task"]["w-a:T1"]["history_count"] == 1

    def test_malformed_messages_skipped(self, monkeypatch):
        """body無しやfrom/task/stateの欠落は無視される"""
        msgs = [
            {"msg_id": 1, "body": None},
            {"msg_id": 2, "body": {"v": 1, "kind": "event", "from": "", "task": "T1", "data": {"type": "state", "state": "ready"}}},
            {"msg_id": 3, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "", "data": {"type": "state", "state": "ready"}}},
            {"msg_id": 4, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "T1", "data": {"type": "state", "state": ""}}},
            {"msg_id": 5, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "T1", "data": {"type": "state", "state": "ready"}}, "created_at": "ok"},
        ]
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: self._build_history(msgs),
        )
        result = ow_service.reconstruct_state_from_relay("ChAbCdEf")
        assert list(result["by_worker_task"]) == ["w-a:T1"]
        assert result["by_worker_task"]["w-a:T1"]["history_count"] == 1
        # max_msg_idは無視されたメッセージのidも含めて最大を返す
        assert result["max_msg_id"] == 5

    def test_history_error_propagates_as_empty(self, monkeypatch):
        """ow_historyがerror dictを返したら、再構築結果はerror付きの空dict"""
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: {"error": {"code": 500, "message": "boom"}},
        )
        result = ow_service.reconstruct_state_from_relay("ChAbCdEf")
        assert result["by_worker_task"] == {}
        assert result["max_msg_id"] == 0
        assert "error" in result

    def test_old_kind_cmd_state_messages_are_skipped(self, monkeypatch):
        """旧形式 kind=cmd/state のレコードは reconstruct_state_from_relay で無視される（v3 cutoff）"""
        msgs = [
            # 旧形式 kind:cmd
            {"msg_id": 1, "body": {"kind": "cmd", "from": "orch", "to": "w-a", "task": "T1", "verb": "assign"}},
            # 旧形式 kind:state
            {"msg_id": 2, "body": {"kind": "state", "from": "w-a", "task": "T1", "state": "ready"}, "created_at": "t2"},
            # 新形式 kind:event data.type:state のみ集計対象
            {"msg_id": 3, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "T1", "data": {"type": "state", "state": "working"}}, "created_at": "t3"},
        ]
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: self._build_history(msgs),
        )
        result = ow_service.reconstruct_state_from_relay("ChAbCdEf")
        # 旧形式はskipされ、新形式の1件のみ集計される
        assert "w-a:T1" in result["by_worker_task"]
        assert result["by_worker_task"]["w-a:T1"]["history_count"] == 1
        assert result["by_worker_task"]["w-a:T1"]["latest_state"] == "working"


# ----------------------------
# detect_crash_inconsistencies
# ----------------------------


class TestDetectCrashInconsistencies:
    """queue × relay最新state × presence の突合分類。"""

    def test_ghost_active_with_working_relay_state(self):
        """queue=working & offline & relay最新=working → ghost_active, suggested=stalled"""
        queue_tasks = [
            {"task": "T1", "title": "x", "status": "working", "worker": "w-a", "term_ref": "x"},
        ]
        reconstructed = {
            "by_worker_task": {
                "w-a:T1": {
                    "alias": "w-a", "task": "T1",
                    "latest_state": "working", "latest_msg_id": 100, "latest_at": "t100",
                    "history_count": 5,
                }
            },
            "max_msg_id": 100,
        }
        detected = ow_service.detect_crash_inconsistencies(queue_tasks, reconstructed, presence=[])
        assert len(detected["ghost_active"]) == 1
        g = detected["ghost_active"][0]
        assert g["alias"] == "w-a"
        assert g["task"] == "T1"
        assert g["queue_status"] == "working"
        assert g["latest_state"] == "working"
        assert g["suggested_status"] == "stalled"

    def test_ghost_active_with_done_relay_state(self):
        """queue=working & offline & relay最新=done → suggested=done（terminal state反映）"""
        queue_tasks = [
            {"task": "T2", "title": "x", "status": "working", "worker": "w-b", "term_ref": "x"},
        ]
        reconstructed = {
            "by_worker_task": {
                "w-b:T2": {
                    "alias": "w-b", "task": "T2",
                    "latest_state": "done", "latest_msg_id": 200, "latest_at": "t200",
                    "history_count": 6,
                }
            },
            "max_msg_id": 200,
        }
        detected = ow_service.detect_crash_inconsistencies(queue_tasks, reconstructed, presence=[])
        assert detected["ghost_active"][0]["suggested_status"] == "done"

    def test_ghost_active_with_no_relay_state(self):
        """queue=working & offline & relay履歴なし → suggested=stalled"""
        queue_tasks = [
            {"task": "T3", "title": "x", "status": "working", "worker": "w-c", "term_ref": "x"},
        ]
        reconstructed = {"by_worker_task": {}, "max_msg_id": 0}
        detected = ow_service.detect_crash_inconsistencies(queue_tasks, reconstructed, presence=[])
        assert len(detected["ghost_active"]) == 1
        assert detected["ghost_active"][0]["latest_state"] is None
        assert detected["ghost_active"][0]["suggested_status"] == "stalled"

    def test_no_inconsistency_when_active_and_online(self):
        """queue=working & online → 整合（ghost_activeに入らない）"""
        queue_tasks = [
            {"task": "T1", "title": "x", "status": "working", "worker": "w-a", "term_ref": "x"},
        ]
        detected = ow_service.detect_crash_inconsistencies(
            queue_tasks, {"by_worker_task": {}, "max_msg_id": 0}, presence=["w-a", "orch"]
        )
        assert detected["ghost_active"] == []
        assert detected["pending_spawn"] == []
        assert detected["stalled_done"] == []
        assert detected["orphans"] == []

    def test_spawning_without_relay_history_is_pending_spawn(self):
        """spawning & offline & relay履歴なし → pending_spawn (起動進行中の可能性、自動更新なし)"""
        queue_tasks = [
            {"task": "T1", "title": "x", "status": "spawning", "worker": "w-a", "term_ref": "(pending)"},
        ]
        detected = ow_service.detect_crash_inconsistencies(
            queue_tasks, {"by_worker_task": {}, "max_msg_id": 0}, presence=[]
        )
        # ghost_activeには入らない
        assert detected["ghost_active"] == []
        assert len(detected["pending_spawn"]) == 1
        p = detected["pending_spawn"][0]
        assert p["alias"] == "w-a"
        assert p["queue_status"] == "spawning"
        assert p["has_relay_history"] is False
        assert p["suggested_status"] is None  # 自動更新対象外

    def test_spawning_with_relay_history_is_pending_spawn_with_suggested(self):
        """spawning & offline & relay最新=working → pending_spawn (has_relay_history=True、suggested=stalled)"""
        queue_tasks = [
            {"task": "T1", "title": "x", "status": "spawning", "worker": "w-a", "term_ref": "(pending)"},
        ]
        reconstructed = {
            "by_worker_task": {
                "w-a:T1": {
                    "alias": "w-a", "task": "T1",
                    "latest_state": "working", "latest_msg_id": 50, "latest_at": "t50",
                    "history_count": 2,
                }
            },
            "max_msg_id": 50,
        }
        detected = ow_service.detect_crash_inconsistencies(queue_tasks, reconstructed, presence=[])
        assert detected["ghost_active"] == []
        assert len(detected["pending_spawn"]) == 1
        p = detected["pending_spawn"][0]
        assert p["has_relay_history"] is True
        assert p["latest_state"] == "working"
        assert p["suggested_status"] == "stalled"

    def test_spawning_online_is_consistent(self):
        """spawning & online → 整合（起動進行中の正常状態）"""
        queue_tasks = [
            {"task": "T1", "title": "x", "status": "spawning", "worker": "w-a", "term_ref": "(pending)"},
        ]
        detected = ow_service.detect_crash_inconsistencies(
            queue_tasks, {"by_worker_task": {}, "max_msg_id": 0}, presence=["w-a"]
        )
        assert detected["ghost_active"] == []
        assert detected["pending_spawn"] == []

    def test_stalled_done_detected(self):
        """queue=done & worker presence online → stalled_done"""
        queue_tasks = [
            {"task": "T1", "title": "x", "status": "done", "worker": "w-a", "term_ref": "x"},
        ]
        detected = ow_service.detect_crash_inconsistencies(
            queue_tasks, {"by_worker_task": {}, "max_msg_id": 0}, presence=["w-a"]
        )
        assert detected["stalled_done"] == [{"task": "T1", "alias": "w-a", "queue_status": "done"}]

    def test_orphan_detected(self):
        """queueに登場しないw-* worker が presence にいる → orphan"""
        queue_tasks = [
            {"task": "T1", "title": "x", "status": "working", "worker": "w-a", "term_ref": "x"},
        ]
        reconstructed = {
            "by_worker_task": {
                "w-z:T9": {
                    "alias": "w-z", "task": "T9",
                    "latest_state": "ready", "latest_msg_id": 50, "latest_at": "t50",
                    "history_count": 1,
                }
            },
            "max_msg_id": 50,
        }
        detected = ow_service.detect_crash_inconsistencies(
            queue_tasks, reconstructed, presence=["w-a", "w-z", "orch"]
        )
        assert len(detected["orphans"]) == 1
        orphan = detected["orphans"][0]
        assert orphan["alias"] == "w-z"
        assert orphan["relay_tasks"] == [
            {"task": "T9", "latest_state": "ready", "latest_msg_id": 50},
        ]

    def test_orch_handle_not_orphan(self):
        """orch handleは w-* で始まらないのでorphan判定対象外"""
        detected = ow_service.detect_crash_inconsistencies(
            queue_tasks=[], reconstructed={"by_worker_task": {}, "max_msg_id": 0},
            presence=["orch"],
        )
        assert detected["orphans"] == []

    def test_multiple_categories_simultaneously(self):
        """4カテゴリが同時発生 → それぞれに正しく分類"""
        queue_tasks = [
            # ghost_active: working & offline
            {"task": "T1", "title": "x", "status": "working", "worker": "w-a", "term_ref": "x"},
            # stalled_done: done & online
            {"task": "T2", "title": "y", "status": "done", "worker": "w-b", "term_ref": "y"},
            # 整合
            {"task": "T3", "title": "z", "status": "working", "worker": "w-c", "term_ref": "z"},
        ]
        reconstructed = {
            "by_worker_task": {
                "w-a:T1": {
                    "alias": "w-a", "task": "T1",
                    "latest_state": "working", "latest_msg_id": 100, "latest_at": "t100",
                    "history_count": 4,
                }
            },
            "max_msg_id": 100,
        }
        detected = ow_service.detect_crash_inconsistencies(
            queue_tasks, reconstructed,
            presence=["w-b", "w-c", "w-z", "orch"],  # w-a offline, w-z orphan
        )
        assert [g["alias"] for g in detected["ghost_active"]] == ["w-a"]
        assert [s["alias"] for s in detected["stalled_done"]] == ["w-b"]
        assert [o["alias"] for o in detected["orphans"]] == ["w-z"]


# ----------------------------
# _apply_queue_status_update
# ----------------------------


class TestApplyQueueStatusUpdate:
    """queueファイルの単一タスクstatus更新（queue層との接点）。"""

    def _setup_queue(self, tmp_path: Path, content: str) -> Path:
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "queue-t454.md").write_text(content)
        return queue_dir

    def test_status_replaced_in_header(self, tmp_path):
        """T1のstatus列が working → stalled に置換される"""
        queue_dir = self._setup_queue(
            tmp_path,
            "## T1 | mytask | working\n"
            "- worker: w-a / term_ref: x / session: y\n"
            "- note: doing\n"
            "\n"
            "## T2 | other | queued\n"
            "- worker: w-b\n",
        )
        ow_service._apply_queue_status_update(
            queue_dir=queue_dir, topic_id="454", task="T1", new_status="stalled",
        )
        content = (queue_dir / "queue-t454.md").read_text()
        assert "## T1 | mytask | stalled\n" in content
        # T2は触らない
        assert "## T2 | other | queued\n" in content

    def test_note_replaces_existing(self, tmp_path):
        """既存 - note: 行があれば置換される"""
        queue_dir = self._setup_queue(
            tmp_path,
            "## T1 | mytask | working\n"
            "- worker: w-a\n"
            "- note: original note\n",
        )
        ow_service._apply_queue_status_update(
            queue_dir=queue_dir, topic_id="454", task="T1",
            new_status="done", note="recovered from relay",
        )
        content = (queue_dir / "queue-t454.md").read_text()
        assert "- note: recovered from relay\n" in content
        assert "original note" not in content

    def test_note_appended_when_missing(self, tmp_path):
        """noteがない場合はブロック末尾に追加される"""
        queue_dir = self._setup_queue(
            tmp_path,
            "## T1 | mytask | working\n"
            "- worker: w-a\n"
            "- cwd: /tmp\n",
        )
        ow_service._apply_queue_status_update(
            queue_dir=queue_dir, topic_id="454", task="T1",
            new_status="done", note="added note",
        )
        content = (queue_dir / "queue-t454.md").read_text()
        assert "- note: added note\n" in content
        assert content.index("- cwd:") < content.index("- note:")

    def test_note_with_newlines_is_sanitized(self, tmp_path):
        """note内の改行は空白に畳まれる（1フィールド=1行不変条件）"""
        queue_dir = self._setup_queue(
            tmp_path,
            "## T1 | mytask | working\n- worker: w-a\n",
        )
        ow_service._apply_queue_status_update(
            queue_dir=queue_dir, topic_id="454", task="T1",
            new_status="done", note="line1\nline2\nline3",
        )
        content = (queue_dir / "queue-t454.md").read_text()
        assert "- note: line1 line2 line3\n" in content

    def test_missing_task_raises_key_error(self, tmp_path):
        """指定taskが存在しない → KeyError"""
        queue_dir = self._setup_queue(
            tmp_path,
            "## T1 | mytask | working\n- worker: w-a\n",
        )
        with pytest.raises(KeyError):
            ow_service._apply_queue_status_update(
                queue_dir=queue_dir, topic_id="454", task="T999", new_status="done",
            )

    def test_missing_file_raises(self, tmp_path):
        """queueファイル不在 → FileNotFoundError"""
        empty_queue_dir = tmp_path / "no-queue"
        empty_queue_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            ow_service._apply_queue_status_update(
                queue_dir=empty_queue_dir, topic_id="454", task="T1", new_status="done",
            )

    def test_invalid_task_format_raises(self, tmp_path):
        """task文字列の形式不正 → ValueError"""
        queue_dir = self._setup_queue(tmp_path, "## T1 | x | working\n")
        with pytest.raises(ValueError):
            ow_service._apply_queue_status_update(
                queue_dir=queue_dir, topic_id="454", task="invalid", new_status="done",
            )


# ----------------------------
# ow_recover
# ----------------------------


class TestOwRecover:
    """crash復旧エントリポイント。"""

    @pytest.fixture(autouse=True)
    def _stub_relay(self, monkeypatch):
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: True)
        # 既定で empty history / presence
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: {"messages": []},
        )
        monkeypatch.setattr(ow_service, "_get_presence", lambda ch: [])

    def test_relay_unreachable_returns_error(self, monkeypatch, tmp_path):
        """relay到達不可 → errorを返す"""
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: False)
        result = ow_service.ow_recover(channel="ChAbCdEf", topic_id="454")
        assert "error" in result
        assert result["error"]["code"] == "RELAY_UNAVAILABLE"

    def test_channel_unavailable_returns_error(self, monkeypatch, tmp_path):
        """channel作成不可 → errorを返す"""
        monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: False)
        result = ow_service.ow_recover(channel="ChAbCdEf", topic_id="454")
        assert "error" in result
        assert result["error"]["code"] == "CHANNEL_UNAVAILABLE"

    def test_dry_run_detects_but_applies_nothing(self, monkeypatch, tmp_path):
        """dry_run=True → queue更新もping送信もしない"""
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "queue-t454.md").write_text(
            "## T1 | x | working\n- worker: w-a / term_ref: x / session: y\n"
        )
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: {
                "messages": [
                    {"msg_id": 1, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "T1", "data": {"type": "state", "state": "working"}}, "created_at": "t1"},
                ]
            },
        )
        send_calls = []
        monkeypatch.setattr(
            ow_service, "ow_send",
            lambda **kw: send_calls.append(kw) or {"msg_id": 99},
        )

        result = ow_service.ow_recover(channel="ChAbCdEf", topic_id="454", dry_run=True)
        assert result["dry_run"] is True
        assert len(result["detected"]["ghost_active"]) == 1
        assert result["applied"]["queue_updates"] == []
        assert result["applied"]["pings_sent"] == []
        assert send_calls == []
        # queueファイルは無変更
        assert "working" in (queue_dir / "queue-t454.md").read_text()

    def test_ghost_active_updates_queue(self, monkeypatch, tmp_path):
        """dry_run=False & ghost_active 検出 → queue status を suggested で更新"""
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "queue-t454.md").write_text(
            "## T1 | mytask | working\n- worker: w-a / term_ref: x / session: y\n"
        )
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: {
                "messages": [
                    {"msg_id": 1, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "T1", "data": {"type": "state", "state": "done"}}, "created_at": "t1"},
                ]
            },
        )

        result = ow_service.ow_recover(channel="ChAbCdEf", topic_id="454")
        assert result["dry_run"] is False
        content = (queue_dir / "queue-t454.md").read_text()
        assert "## T1 | mytask | done\n" in content
        assert len(result["applied"]["queue_updates"]) == 1
        assert result["applied"]["queue_updates"][0] == {
            "task": "T1", "alias": "w-a", "from": "working", "to": "done",
        }

    def test_orphans_trigger_ping(self, monkeypatch, tmp_path):
        """orphan worker検出 → cmd:ping送信"""
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "queue-t454.md").write_text("")
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))
        monkeypatch.setattr(ow_service, "_get_presence", lambda ch: ["w-z"])

        sent = []
        monkeypatch.setattr(
            ow_service, "ow_send",
            lambda **kw: sent.append(kw) or {"msg_id": 99},
        )
        result = ow_service.ow_recover(channel="ChAbCdEf", topic_id="454")
        assert len(result["applied"]["pings_sent"]) == 1
        assert result["applied"]["pings_sent"][0]["alias"] == "w-z"
        assert result["applied"]["pings_sent"][0]["reason"] == "orphan"
        assert sent and sent[0]["body"]["data"]["type"] == "ping"
        assert sent[0]["body"]["to"] == "w-z"
        assert sent[0]["needs_reply"] is True

    def test_stalled_done_triggers_ping(self, monkeypatch, tmp_path):
        """stalled_done検出 → cmd:ping送信（task引数付き）"""
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "queue-t454.md").write_text(
            "## T1 | x | done\n- worker: w-a / term_ref: x / session: y\n"
        )
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))
        monkeypatch.setattr(ow_service, "_get_presence", lambda ch: ["w-a"])
        sent = []
        monkeypatch.setattr(
            ow_service, "ow_send",
            lambda **kw: sent.append(kw) or {"msg_id": 99},
        )
        result = ow_service.ow_recover(channel="ChAbCdEf", topic_id="454")
        assert any(p["reason"] == "stalled_done" for p in result["applied"]["pings_sent"])
        assert sent[0]["body"]["task"] == "T1"

    def test_pending_spawn_without_history_is_not_touched(self, monkeypatch, tmp_path):
        """spawning & 履歴ゼロ → pending_spawn検出のみ、queue更新もping送信もしない (C1対応)"""
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "queue-t454.md").write_text(
            "## T1 | mytask | spawning\n- worker: w-a / term_ref: (pending) / session: (pending)\n"
        )
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))
        # 履歴空のまま (worker起動進行中のシナリオ)
        send_calls = []
        monkeypatch.setattr(
            ow_service, "ow_send",
            lambda **kw: send_calls.append(kw) or {"msg_id": 99},
        )
        before = (queue_dir / "queue-t454.md").read_text()

        result = ow_service.ow_recover(channel="ChAbCdEf", topic_id="454")
        assert len(result["detected"]["pending_spawn"]) == 1
        assert result["detected"]["pending_spawn"][0]["has_relay_history"] is False
        # 自動更新なし
        assert result["applied"]["queue_updates"] == []
        # queueファイルは無変更
        assert (queue_dir / "queue-t454.md").read_text() == before

    def test_pending_spawn_with_history_is_updated(self, monkeypatch, tmp_path):
        """spawning & relay最新=done → pending_spawn (has_relay_history=True) → queue=done"""
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "queue-t454.md").write_text(
            "## T1 | mytask | spawning\n- worker: w-a / term_ref: (pending) / session: (pending)\n"
        )
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: {
                "messages": [
                    {"msg_id": 1, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "T1", "data": {"type": "state", "state": "done"}}, "created_at": "t1"},
                ]
            },
        )

        result = ow_service.ow_recover(channel="ChAbCdEf", topic_id="454")
        # pending_spawnに分類されるがhas_relay_history=Trueなので自動更新対象
        assert len(result["detected"]["pending_spawn"]) == 1
        assert result["detected"]["pending_spawn"][0]["has_relay_history"] is True
        assert len(result["applied"]["queue_updates"]) == 1
        assert result["applied"]["queue_updates"][0] == {
            "task": "T1", "alias": "w-a", "from": "spawning", "to": "done",
        }
        new_content = (queue_dir / "queue-t454.md").read_text()
        assert "## T1 | mytask | done\n" in new_content

    def test_relay_history_fetch_failure_returns_empty_detected(self, monkeypatch, tmp_path):
        """ensure_relay_serverは成功するがow_historyが失敗 → detected全空 + warningsに記録、queue未変更"""
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        original = "## T1 | mytask | working\n- worker: w-a / term_ref: x / session: y\n"
        (queue_dir / "queue-t454.md").write_text(original)
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: {"error": {"code": 500, "message": "timeout"}},
        )

        result = ow_service.ow_recover(channel="ChAbCdEf", topic_id="454")
        assert result["detected"]["ghost_active"] == []
        assert result["detected"]["pending_spawn"] == []
        assert result["detected"]["stalled_done"] == []
        assert result["detected"]["orphans"] == []
        assert any("relay history fetch error" in w for w in result["warnings"])
        # queueファイルは無変更
        assert (queue_dir / "queue-t454.md").read_text() == original

    def test_queue_update_failure_recorded_as_warning(self, monkeypatch, tmp_path):
        """queue更新中の例外はwarningsに記録され処理は継続する"""
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "queue-t454.md").write_text(
            "## T1 | mytask | working\n- worker: w-a / term_ref: x / session: y\n"
        )
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(queue_dir))
        monkeypatch.setattr(
            ow_service, "ow_history",
            lambda channel, since=0, limit=10000: {
                "messages": [
                    {"msg_id": 1, "body": {"v": 1, "kind": "event", "from": "w-a", "task": "T1", "data": {"type": "state", "state": "done"}}, "created_at": "t1"},
                ]
            },
        )

        def fake_apply(**kwargs):
            raise OSError("disk full")
        monkeypatch.setattr(ow_service, "_apply_queue_status_update", fake_apply)

        result = ow_service.ow_recover(channel="ChAbCdEf", topic_id="454")
        assert result["applied"]["queue_updates"] == []
        assert any("disk full" in w for w in result["warnings"])


# ----------------------------
# _get_presence (HTTPラッパー fail-soft)
# ----------------------------


class TestGetPresence:
    def test_success(self, monkeypatch):
        """正常応答 → handles配列を返す"""
        monkeypatch.setattr(
            ow_service, "_relay_request",
            lambda method, path: {"handles": ["orch", "w-a"]},
        )
        assert ow_service._get_presence("ChAbCdEf") == ["orch", "w-a"]

    def test_error_dict_returns_empty(self, monkeypatch):
        """relayがerror dictを返す → 空リスト"""
        monkeypatch.setattr(
            ow_service, "_relay_request",
            lambda method, path: {"error": {"code": 500}},
        )
        assert ow_service._get_presence("ChAbCdEf") == []

    def test_exception_returns_empty(self, monkeypatch):
        """_relay_request例外 → 空リスト（例外を伝播しない）"""
        def boom(method, path):
            raise RuntimeError("net down")
        monkeypatch.setattr(ow_service, "_relay_request", boom)
        assert ow_service._get_presence("ChAbCdEf") == []

    def test_missing_handles_key(self, monkeypatch):
        """handlesキーがないレスポンス → 空リスト"""
        monkeypatch.setattr(ow_service, "_relay_request", lambda method, path: {})
        assert ow_service._get_presence("ChAbCdEf") == []


# ----------------------------
# ow_spawn_worker preflight integration
# ----------------------------


class TestOwSpawnWorkerPreflight:
    """ow_spawn_workerがpreflight失敗時にspawn中止+warningsを返すこと。"""

    def test_relay_unreachable_returns_precondition_failed(self, monkeypatch, tmp_path):
        """relay不可 → SPAWN_PRECONDITION_FAILEDで止まる（task fileやアダプタは呼ばれない）"""
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: False)
        write_called = []
        monkeypatch.setattr(
            ow_service, "_write_task_file",
            lambda **kwargs: write_called.append(True) or Path("/tmp/x.md"),
        )

        result = ow_service.ow_spawn_worker(
            alias="w-x", channel="ChAbCdEf", cwd=str(tmp_path),
            model="claude-opus-4-7",
        )
        assert "error" in result
        assert result["error"]["code"] == "SPAWN_PRECONDITION_FAILED"
        assert any("relay" in w for w in result["error"]["warnings"])
        assert write_called == []

    def test_alias_collision_blocks_spawn(self, monkeypatch, tmp_path):
        """presenceに同aliasがいる → SPAWN_PRECONDITION_FAILED"""
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: True)
        monkeypatch.setattr(ow_service, "_get_presence", lambda ch: ["w-x"])
        monkeypatch.setattr(ow_service, "ow_get_identity", lambda ch, h: None)
        result = ow_service.ow_spawn_worker(
            alias="w-x", channel="ChAbCdEf", cwd=str(tmp_path),
            model="claude-opus-4-7",
        )
        assert "error" in result
        assert result["error"]["code"] == "SPAWN_PRECONDITION_FAILED"
        assert any("alias w-x" in w for w in result["error"]["warnings"])
