"""ow_service: alias 書式バリデーションと worker workspace 用 settings.local.json 生成のテスト。

- `_validate_alias_format`: 単独でのOK/NG分類（最小長 + kebab-case regex）
- `_validate_spawn_preconditions` 経由での alias 書式違反 → ok=False
- `ow_spawn_worker` 経由での書式違反 → SPAWN_PRECONDITION_FAILED
- `_ensure_worker_askuser_deny`: 新規作成 / 既存 merge / dedup / 壊れた JSON 上書き
- `ow_spawn_worker` が cwd 配下に `.claude/settings.local.json` を生成して
  `permissions.deny` に `"AskUserQuestion"` を追記すること
"""
import json
from pathlib import Path

import pytest

from src.services import ow_service


# ----------------------------
# _validate_alias_format
# ----------------------------


class TestValidateAliasFormat:
    """alias の書式（min-length 8 + kebab-case）のユニットテスト"""

    @pytest.mark.parametrize(
        "alias",
        [
            "w-playbook",      # 10 chars
            "w-alpha01",       # 9 chars, digit suffix OK
            "w-tinyworker",    # 12 chars
            "worker01",        # 8 chars exact
            "abcdefgh",        # 8 chars, no hyphen
            "w-a-b-c-d",       # 9 chars, multi-hyphen OK (末尾は英字)
        ],
    )
    def test_valid_aliases_return_none(self, alias: str):
        assert ow_service._validate_alias_format(alias) is None

    @pytest.mark.parametrize(
        "alias",
        ["", "w", "w-a", "w-tiny", "w-abcde", "abcdefg"],  # all < 8 chars
    )
    def test_too_short_aliases_rejected(self, alias: str):
        err = ow_service._validate_alias_format(alias)
        assert err is not None
        # 空文字は別メッセージ
        if alias:
            assert "too short" in err

    @pytest.mark.parametrize(
        "alias",
        [
            "W-playbook",      # 大文字始まり
            "w-Playbook",      # 大文字混入
            "w_playbook",      # アンダースコア禁止
            "-playbook",       # 先頭ハイフン
            "playbook-",       # 末尾ハイフン
            "1playbook",       # 先頭が数字
            "w-play book",     # スペース
            "w-play.book",     # ドット
            "w-プレイブック",   # 非ASCII
            "w--playbook",     # 連続ハイフン
            "abc---def",       # 3連続ハイフン
        ],
    )
    def test_invalid_kebab_case_rejected(self, alias: str):
        err = ow_service._validate_alias_format(alias)
        assert err is not None

    def test_non_string_rejected(self):
        # 型ガードも兼ねる
        assert ow_service._validate_alias_format(None) is not None  # type: ignore[arg-type]


# ----------------------------
# _validate_spawn_preconditions: alias 書式違反は warning に積まれる
# ----------------------------


class TestSpawnPreconditionsAliasFormat:
    @pytest.fixture(autouse=True)
    def _allow_other_checks(self, monkeypatch):
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: True)
        monkeypatch.setattr(ow_service, "_get_presence", lambda ch: [])
        monkeypatch.setattr(ow_service, "ow_get_identity", lambda ch, h: None)
        # ow_spawn_worker は spawning event を broadcast するため _relay_request を no-op に
        # 差し替えて、relay HTTP call をテストから切り離す。
        monkeypatch.setattr(
            ow_service,
            "_relay_request",
            lambda *args, **kwargs: {"msg_id": 0},
        )

    def test_too_short_alias_yields_warning(self, tmp_path):
        result = ow_service._validate_spawn_preconditions(
            alias="w-a", channel="ChAbCdEf", cwd=str(tmp_path)
        )
        assert result["ok"] is False
        assert any("too short" in w for w in result["warnings"])

    def test_invalid_charset_alias_yields_warning(self, tmp_path):
        result = ow_service._validate_spawn_preconditions(
            alias="W-Playbook", channel="ChAbCdEf", cwd=str(tmp_path)
        )
        assert result["ok"] is False
        assert any("kebab-case" in w for w in result["warnings"])

    def test_valid_alias_passes(self, tmp_path):
        result = ow_service._validate_spawn_preconditions(
            alias="w-playbook", channel="ChAbCdEf", cwd=str(tmp_path)
        )
        assert result["ok"] is True
        assert result["warnings"] == []

    def test_invalid_alias_does_not_contact_relay(self, monkeypatch, tmp_path):
        """alias 書式違反時は relay 接続 (ensure_relay_server / ensure_channel) を呼ばない。

        コメントが意図する早期 return の挙動を call_count で検証する。
        """
        relay_calls: list[None] = []
        channel_calls: list[str] = []

        def _track_relay() -> bool:
            relay_calls.append(None)
            return True

        def _track_channel(ch: str) -> bool:
            channel_calls.append(ch)
            return True

        monkeypatch.setattr(ow_service, "ensure_relay_server", _track_relay)
        monkeypatch.setattr(ow_service, "ensure_channel", _track_channel)

        result = ow_service._validate_spawn_preconditions(
            alias="w-a", channel="ChAbCdEf", cwd=str(tmp_path)
        )
        assert result["ok"] is False
        assert relay_calls == []
        assert channel_calls == []


# ----------------------------
# ow_spawn_worker: alias 書式違反は SPAWN_PRECONDITION_FAILED
# ----------------------------


class TestSpawnWorkerAliasFormat:
    @pytest.fixture(autouse=True)
    def _allow_other_checks(self, monkeypatch):
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: True)
        monkeypatch.setattr(ow_service, "_get_presence", lambda ch: [])
        monkeypatch.setattr(ow_service, "ow_get_identity", lambda ch, h: None)
        # ow_spawn_worker は spawning event を broadcast するため _relay_request を no-op に
        # 差し替えて、relay HTTP call をテストから切り離す。
        monkeypatch.setattr(
            ow_service,
            "_relay_request",
            lambda *args, **kwargs: {"msg_id": 0},
        )
        # 万が一 preflight をすり抜けても /tmp などを汚さない
        monkeypatch.setattr(ow_service, "_ensure_worker_askuser_deny", lambda c: None)
        monkeypatch.delenv("OW_TERMINAL", raising=False)

    def test_too_short_alias_returns_precondition_failed(self, monkeypatch, tmp_path):
        result = ow_service.ow_spawn_worker(
            alias="w-a",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="d", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "SPAWN_PRECONDITION_FAILED"
        assert any("too short" in w for w in result["error"]["warnings"])

    def test_uppercase_alias_returns_precondition_failed(self, monkeypatch, tmp_path):
        result = ow_service.ow_spawn_worker(
            alias="W-Playbook",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="d", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "SPAWN_PRECONDITION_FAILED"
        assert any("kebab-case" in w for w in result["error"]["warnings"])

    def test_underscore_alias_returns_precondition_failed(self, monkeypatch, tmp_path):
        result = ow_service.ow_spawn_worker(
            alias="w_playbook",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="d", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "SPAWN_PRECONDITION_FAILED"

    def test_valid_alias_proceeds_past_precondition(self, monkeypatch, tmp_path):
        """正常 alias なら manual fallback まで進める（preflight でブロックされない）"""
        result = ow_service.ow_spawn_worker(
            alias="w-playbook",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="d", task_n=1,
        )
        assert result.get("manual") is True


# ----------------------------
# ow_spawn_worker: spawning broadcast 失敗時は SPAWN_PRECONDITION_FAILED
# ----------------------------


class TestSpawnWorkerSpawningBroadcastFailure:
    """relay への event:state(spawning) broadcast 失敗時の挙動を確認する。

    新真実源モデルでは relay events が真実源のため、broadcast 失敗は spawn 中止。
    """

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: True)
        monkeypatch.setattr(ow_service, "_get_presence", lambda ch: [])
        monkeypatch.setattr(ow_service, "ow_get_identity", lambda ch, h: None)
        monkeypatch.setattr(ow_service, "_ensure_worker_askuser_deny", lambda c: None)
        monkeypatch.delenv("OW_TERMINAL", raising=False)

    def test_relay_error_returns_precondition_failed(self, monkeypatch, tmp_path):
        """relay が 5xx を返して broadcast 失敗 → SPAWN_PRECONDITION_FAILED。"""
        monkeypatch.setattr(
            ow_service,
            "_relay_request",
            lambda *args, **kwargs: {"error": {"code": 503, "message": "relay unavailable"}},
        )
        result = ow_service.ow_spawn_worker(
            alias="w-playbook",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="d", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "SPAWN_PRECONDITION_FAILED"
        assert any("spawning" in w for w in result["error"]["warnings"])

    def test_relay_error_warnings_contain_alias_and_detail(self, monkeypatch, tmp_path):
        """broadcast 失敗の警告メッセージに alias と relay エラー詳細が含まれる。"""
        monkeypatch.setattr(
            ow_service,
            "_relay_request",
            lambda *args, **kwargs: {"error": {"code": 503, "message": "relay down"}},
        )
        result = ow_service.ow_spawn_worker(
            alias="w-playbook",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="d", task_n=1,
        )
        warnings = result["error"]["warnings"]
        assert any("w-playbook" in w for w in warnings)


# ----------------------------
# _ensure_worker_askuser_deny: settings.local.json 生成 / merge
# ----------------------------


class TestEnsureWorkerAskUserDeny:
    def test_creates_new_file_when_missing(self, tmp_path):
        ow_service._ensure_worker_askuser_deny(str(tmp_path))
        settings_path = tmp_path / ".claude" / "settings.local.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data == {"permissions": {"deny": ["AskUserQuestion"]}}

    def test_creates_claude_subdir_if_missing(self, tmp_path):
        ow_service._ensure_worker_askuser_deny(str(tmp_path))
        assert (tmp_path / ".claude").is_dir()

    def test_appends_to_existing_deny_list(self, tmp_path):
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        settings_path = settings_dir / "settings.local.json"
        settings_path.write_text(
            json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}}),
            encoding="utf-8",
        )

        ow_service._ensure_worker_askuser_deny(str(tmp_path))

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        # 既存の deny は保持
        assert "Bash(rm:*)" in data["permissions"]["deny"]
        # AskUserQuestion が追加されている
        assert "AskUserQuestion" in data["permissions"]["deny"]

    def test_does_not_duplicate_askuserquestion(self, tmp_path):
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        settings_path = settings_dir / "settings.local.json"
        settings_path.write_text(
            json.dumps({"permissions": {"deny": ["AskUserQuestion"]}}),
            encoding="utf-8",
        )

        ow_service._ensure_worker_askuser_deny(str(tmp_path))

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["permissions"]["deny"].count("AskUserQuestion") == 1

    def test_preserves_other_top_level_keys(self, tmp_path):
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        settings_path = settings_dir / "settings.local.json"
        settings_path.write_text(
            json.dumps({"env": {"FOO": "bar"}, "permissions": {"allow": ["Read"]}}),
            encoding="utf-8",
        )

        ow_service._ensure_worker_askuser_deny(str(tmp_path))

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["env"] == {"FOO": "bar"}
        assert data["permissions"]["allow"] == ["Read"]
        assert data["permissions"]["deny"] == ["AskUserQuestion"]

    def test_overwrites_when_existing_is_invalid_json(self, tmp_path):
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        settings_path = settings_dir / "settings.local.json"
        settings_path.write_text("not-json{{{", encoding="utf-8")

        ow_service._ensure_worker_askuser_deny(str(tmp_path))

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data == {"permissions": {"deny": ["AskUserQuestion"]}}

    def test_overwrites_when_existing_is_non_dict_json(self, tmp_path):
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        settings_path = settings_dir / "settings.local.json"
        settings_path.write_text(json.dumps(["a", "b"]), encoding="utf-8")

        ow_service._ensure_worker_askuser_deny(str(tmp_path))

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data == {"permissions": {"deny": ["AskUserQuestion"]}}

    def test_silently_skips_when_cwd_does_not_exist(self, tmp_path):
        """cwd が存在しない場合は warning ログのみで例外なく終了する"""
        missing = tmp_path / "does-not-exist"
        # 例外を投げない
        ow_service._ensure_worker_askuser_deny(str(missing))
        # ディレクトリも作らない
        assert not (missing / ".claude").exists()


# ----------------------------
# ow_spawn_worker integration: settings.local.json が cwd 配下に生成される
# ----------------------------


class TestSpawnWorkerWritesSettingsLocal:
    @pytest.fixture(autouse=True)
    def _stub_preflight(self, monkeypatch):
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda c: True)
        monkeypatch.setattr(ow_service, "_get_presence", lambda c: [])
        monkeypatch.setattr(ow_service, "ow_get_identity", lambda ch, h: None)
        monkeypatch.setattr(
            ow_service,
            "_relay_request",
            lambda *args, **kwargs: {"msg_id": 0},
        )
        monkeypatch.delenv("OW_TERMINAL", raising=False)

    def test_spawn_creates_settings_local_under_cwd(self, monkeypatch, tmp_path):
        worker_cwd = tmp_path / "worker-ws"
        worker_cwd.mkdir()
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()

        result = ow_service.ow_spawn_worker(
            alias="w-playbook",
            channel="ch1",
            cwd=str(worker_cwd),
            model="claude-opus-4-7",
            task_title="t", acceptance="d", task_n=1,
        )
        assert result.get("manual") is True

        settings_path = worker_cwd / ".claude" / "settings.local.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "AskUserQuestion" in data["permissions"]["deny"]

    def test_spawn_merges_into_existing_settings(self, monkeypatch, tmp_path):
        worker_cwd = tmp_path / "worker-ws"
        (worker_cwd / ".claude").mkdir(parents=True)
        settings_path = worker_cwd / ".claude" / "settings.local.json"
        settings_path.write_text(
            json.dumps({"permissions": {"allow": ["Read"], "deny": ["Bash(rm:*)"]}}),
            encoding="utf-8",
        )
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()

        result = ow_service.ow_spawn_worker(
            alias="w-playbook",
            channel="ch1",
            cwd=str(worker_cwd),
            model="claude-opus-4-7",
            task_title="t", acceptance="d", task_n=1,
        )
        assert result.get("manual") is True

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["permissions"]["allow"] == ["Read"]
        assert "Bash(rm:*)" in data["permissions"]["deny"]
        assert "AskUserQuestion" in data["permissions"]["deny"]
