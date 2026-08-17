"""session_aliases.json（src.services.session_registry_service）のユニットテスト。

CLI session の解決（relay_identity.resolve_cli_session / cli_session の
is_process_alive・read_cli_session）は外部境界としてFakeCliWorld経由でmockし、
ファイルI/O・ロック・alias生成・衝突解決・GCは実ファイルで検証する。
"""
import datetime as dt
import itertools
import json
import threading
import time

import pytest

from src.services import session_registry_service as srs


class FakeCliWorld:
    """resolve_cli_session / is_process_alive / read_cli_session をまとめて偽装する。"""

    def __init__(self):
        self._alive_pids: set[int] = set()
        self._by_pid: dict[int, dict] = {}
        self._by_bridge: dict[str, dict] = {}

    def add(self, bridge_session_id, pid, cli_session_id, name="workspace-a1", cwd=None, cli_status=None):
        info = {
            "cli_pid": pid,
            "name": name,
            "cli_session_id": cli_session_id,
            "cwd": cwd,
            "cli_status": cli_status,
        }
        self._alive_pids.add(pid)
        self._by_pid[pid] = info
        self._by_bridge[bridge_session_id] = info
        return info

    def kill(self, pid):
        self._alive_pids.discard(pid)

    def is_process_alive(self, pid):
        return pid in self._alive_pids

    def read_cli_session(self, pid):
        if pid not in self._alive_pids:
            return None
        return self._by_pid.get(pid)

    def resolve_cli_session(self, bridge_session_id):
        info = self._by_bridge.get(bridge_session_id)
        if info is None or info["cli_pid"] not in self._alive_pids:
            return None
        return info


@pytest.fixture
def world(monkeypatch):
    w = FakeCliWorld()
    monkeypatch.setattr(srs.relay_identity, "resolve_cli_session", w.resolve_cli_session)
    monkeypatch.setattr(srs, "is_process_alive", w.is_process_alive)
    monkeypatch.setattr(srs.cli_session, "read_cli_session", w.read_cli_session)
    return w


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    path = tmp_path / "session_aliases.json"
    monkeypatch.setenv(srs.REGISTRY_PATH_ENV, str(path))
    return path


def _sequential_timestamps(monkeypatch, count=200):
    """_now_iso() が呼ばれるたびに1秒ずつ進む決定的なタイムスタンプ列を差し込む。

    updated_at の大小比較に依存するテスト（GC上限・sort順）は、実時刻だと
    同一秒内の複数呼び出しでタイムスタンプが衝突しうるため、これを使う。
    基点は現在時刻（GCのTTL判定 `datetime.now(timezone.utc)` は差し替えないため、
    固定の過去日付を基点にすると7日TTLに引っかかってGCされてしまう）。
    """
    base = dt.datetime.now(dt.timezone.utc)
    values = iter(
        (base + dt.timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(count)
    )
    monkeypatch.setattr(srs, "_now_iso", lambda: next(values))


class TestDeriveAlias:
    def test_truncates_over_24_chars_with_ellipsis(self):
        title = "あ" * 30
        alias = srs.derive_alias(title, activity_id=1)
        assert alias == "あ" * srs.ALIAS_MAX_CHARS + "…"

    def test_short_title_is_kept_as_is(self):
        assert srs.derive_alias("短いタイトル", activity_id=1) == "短いタイトル"

    def test_removes_control_characters(self):
        assert srs.derive_alias("foo\x01bar", activity_id=1) == "foobar"

    def test_empty_title_falls_back_to_activity_id(self):
        assert srs.derive_alias("", activity_id=42) == "activity-42"

    def test_whitespace_only_title_falls_back_to_activity_id(self):
        assert srs.derive_alias("   ", activity_id=42) == "activity-42"

    def test_preserves_discussion_prefix(self):
        alias = srs.derive_alias("[議論] session alias 設計", activity_id=1)
        assert alias.startswith("[議論]")


class TestRegisterCheckinCollision:
    def test_self_reregister_same_activity_does_not_shift_alias(self, world, registry_path):
        world.add("bridge-a", pid=100, cli_session_id="cli-1")
        r1 = srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        r2 = srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        assert r1["alias"] == "Foo"
        assert r2["alias"] == "Foo"
        assert r2["collided"] is False

    def test_second_session_same_title_gets_dash_2_suffix(self, world, registry_path):
        world.add("bridge-a", pid=100, cli_session_id="cli-1")
        world.add("bridge-b", pid=200, cli_session_id="cli-2")
        srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        r2 = srs.register_checkin(
            bridge_session_id="bridge-b", activity_id=2, activity_title="Foo", activity_status="in_progress"
        )
        assert r2["alias"] == "Foo-2"
        assert r2["collided"] is True

    def test_third_session_same_title_gets_dash_3_suffix(self, world, registry_path):
        world.add("bridge-a", pid=100, cli_session_id="cli-1")
        world.add("bridge-b", pid=200, cli_session_id="cli-2")
        world.add("bridge-c", pid=300, cli_session_id="cli-3")
        srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        srs.register_checkin(
            bridge_session_id="bridge-b", activity_id=2, activity_title="Foo", activity_status="in_progress"
        )
        r3 = srs.register_checkin(
            bridge_session_id="bridge-c", activity_id=3, activity_title="Foo", activity_status="in_progress"
        )
        assert r3["alias"] == "Foo-3"


class TestManualAliasLifetime:
    def test_manual_alias_survives_recheckin_of_same_activity(self, world, registry_path):
        world.add("bridge-a", pid=100, cli_session_id="cli-1")
        srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        set_result = srs.set_alias(bridge_session_id="bridge-a", alias="MyAlias")
        assert set_result["alias"] == "MyAlias"

        r = srs.register_checkin(
            bridge_session_id="bridge-a",
            activity_id=1,
            activity_title="Foo (更新されたタイトル)",
            activity_status="in_progress",
        )
        assert r["alias"] == "MyAlias"

    def test_manual_alias_discarded_on_different_activity(self, world, registry_path):
        world.add("bridge-a", pid=100, cli_session_id="cli-1")
        srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        srs.set_alias(bridge_session_id="bridge-a", alias="MyAlias")

        r = srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=2, activity_title="Bar", activity_status="in_progress"
        )
        assert r["alias"] == "Bar"


class TestRegisterCheckinGc:
    def test_dead_pid_entry_gced_on_write_and_absent_from_read(self, world, registry_path):
        world.add("bridge-a", pid=100, cli_session_id="cli-1", name="workspace-a1")
        srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        world.kill(100)

        world.add("bridge-b", pid=200, cli_session_id="cli-2", name="workspace-b1")
        srs.register_checkin(
            bridge_session_id="bridge-b", activity_id=2, activity_title="Bar", activity_status="in_progress"
        )

        data = json.loads(registry_path.read_text(encoding="utf-8"))
        assert "cli-1" not in data["sessions"]
        assert "cli-2" in data["sessions"]

        sessions = srs.list_sessions()
        assert [s["activity_title"] for s in sessions] == ["Bar"]

    def test_ttl_expired_entry_is_gced(self, world, registry_path, monkeypatch):
        _sequential_timestamps(monkeypatch)
        world.add("bridge-old", pid=100, cli_session_id="cli-1", name="workspace-a1")
        srs.register_checkin(
            bridge_session_id="bridge-old", activity_id=1, activity_title="Old", activity_status="in_progress"
        )
        data = json.loads(registry_path.read_text(encoding="utf-8"))
        data["sessions"]["cli-1"]["updated_at"] = "2000-01-01T00:00:00Z"
        registry_path.write_text(json.dumps(data), encoding="utf-8")

        world.add("bridge-new", pid=200, cli_session_id="cli-2", name="workspace-b1")
        srs.register_checkin(
            bridge_session_id="bridge-new", activity_id=2, activity_title="New", activity_status="in_progress"
        )

        data = json.loads(registry_path.read_text(encoding="utf-8"))
        assert "cli-1" not in data["sessions"]
        assert "cli-2" in data["sessions"]

    def test_overflow_evicts_oldest_updated_at_first(self, world, registry_path, monkeypatch):
        _sequential_timestamps(monkeypatch, count=srs._MAX_ENTRIES + 10)
        total = srs._MAX_ENTRIES + 1
        for i in range(total):
            pid = 1000 + i
            cli_id = f"cli-{i}"
            world.add(f"bridge-{i}", pid=pid, cli_session_id=cli_id, name=f"workspace-{i}")
            srs.register_checkin(
                bridge_session_id=f"bridge-{i}",
                activity_id=i,
                activity_title=f"Title {i}",
                activity_status="in_progress",
            )

        data = json.loads(registry_path.read_text(encoding="utf-8"))
        assert len(data["sessions"]) == srs._MAX_ENTRIES
        assert "cli-0" not in data["sessions"]  # 最古のupdated_atとして落ちる
        assert f"cli-{total - 1}" in data["sessions"]  # 最新は残る


class TestRegisterCheckinUnresolved:
    def test_returns_none_and_creates_no_file_when_cli_unresolved(self, registry_path, monkeypatch):
        monkeypatch.setattr(srs.relay_identity, "resolve_cli_session", lambda bridge_session_id: None)
        result = srs.register_checkin(
            bridge_session_id="bridge-x", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        assert result is None
        assert not registry_path.exists()

    def test_returns_none_when_bridge_session_id_is_none(self, registry_path):
        result = srs.register_checkin(
            bridge_session_id=None, activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        assert result is None
        assert not registry_path.exists()


class TestConcurrentRegister:
    def test_two_threads_registering_different_sessions_both_persist(
        self, world, registry_path, monkeypatch
    ):
        """flockによる排他が無いと、read-modify-writeの競合で片方の行が消える
        （_load後にsleepを挟み、ロック無しなら重なる時間窓を作る）。
        """
        original_load = srs._load

        def slow_load():
            data = original_load()
            time.sleep(0.05)
            return data

        monkeypatch.setattr(srs, "_load", slow_load)

        world.add("bridge-a", pid=100, cli_session_id="cli-1", name="workspace-a1")
        world.add("bridge-b", pid=200, cli_session_id="cli-2", name="workspace-b1")

        def run(bridge_session_id, activity_id, title):
            srs.register_checkin(
                bridge_session_id=bridge_session_id,
                activity_id=activity_id,
                activity_title=title,
                activity_status="in_progress",
            )

        t1 = threading.Thread(target=run, args=("bridge-a", 1, "Foo"))
        t2 = threading.Thread(target=run, args=("bridge-b", 2, "Bar"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        data = json.loads(registry_path.read_text(encoding="utf-8"))
        assert "cli-1" in data["sessions"]
        assert "cli-2" in data["sessions"]


class TestListSessions:
    def test_sorted_descending_with_self_flag(self, world, registry_path, monkeypatch):
        _sequential_timestamps(monkeypatch)
        world.add("bridge-a", pid=100, cli_session_id="cli-1", name="workspace-a1")
        world.add("bridge-b", pid=200, cli_session_id="cli-2", name="workspace-b1")
        srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="First", activity_status="in_progress"
        )
        srs.register_checkin(
            bridge_session_id="bridge-b", activity_id=2, activity_title="Second", activity_status="in_progress"
        )

        sessions = srs.list_sessions(self_bridge_session_id="bridge-b")
        assert [s["activity_title"] for s in sessions] == ["Second", "First"]
        assert sessions[0]["is_self"] is True
        assert sessions[1]["is_self"] is False

    def test_empty_registry_returns_empty_list(self, registry_path):
        assert srs.list_sessions() == []

    def test_no_write_when_nothing_to_gc(self, world, registry_path):
        """全セッションが生存中でGC対象が無い場合、list_sessionsはファイルを
        書き換えない（get_sessionsは読み取り専用ツールであるべきため）"""
        world.add("bridge-a", pid=100, cli_session_id="cli-1", name="workspace-a1")
        srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        before = registry_path.stat()

        srs.list_sessions()

        after = registry_path.stat()
        assert after.st_ino == before.st_ino
        assert after.st_mtime_ns == before.st_mtime_ns

    def test_write_persists_when_list_sessions_gcs_dead_entry(self, world, registry_path):
        """死亡PIDの行が残っている状態でlist_sessionsを呼ぶと、GC結果がファイルにも
        反映される（読み取り経由のGCも永続化されること）"""
        world.add("bridge-a", pid=100, cli_session_id="cli-1", name="workspace-a1")
        srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        world.kill(100)

        sessions = srs.list_sessions()
        assert sessions == []

        data = json.loads(registry_path.read_text(encoding="utf-8"))
        assert "cli-1" not in data["sessions"]
