"""ow_service: alias 書式バリデーションと worker workspace 用 settings.local.json 生成のテスト。

- `_validate_alias_format`: 単独でのOK/NG分類（prefix `w-` + min-length 8 + kebab-case regex）
- `_suggest_alias`: 書式違反 alias から修正候補の組み立て
- `_validate_spawn_preconditions` 経由での alias 書式違反 → ok=False + Suggested 添付
- `ow_spawn_worker` 経由での書式違反 → SPAWN_PRECONDITION_FAILED + Suggested 添付
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
    """alias 書式 (prefix `w-` + min-length 8 + kebab-case) のユニットテスト"""

    @pytest.mark.parametrize(
        "alias",
        [
            "w-playbook",      # 10 chars
            "w-alpha01",       # 9 chars, digit suffix OK
            "w-tinyworker",    # 12 chars
            "w-design-1064",   # 推奨形式そのもの (purpose+activity_id)
            "w-a-b-c-d",       # 9 chars, multi-hyphen OK (末尾は英字)
        ],
    )
    def test_valid_aliases_return_none(self, alias: str):
        assert ow_service._validate_alias_format(alias) is None

    @pytest.mark.parametrize(
        "alias",
        ["w-a", "w-tiny", "w-abcde"],  # prefix OK, length < 8
    )
    def test_too_short_aliases_rejected_with_recommendation(self, alias: str):
        err = ow_service._validate_alias_format(alias)
        assert err is not None
        assert "too short" in err
        # Friendly Error: 推奨命名規約がメッセージに含まれる
        assert "Recommended" in err
        assert "w-<purpose>" in err

    @pytest.mark.parametrize(
        "alias",
        [
            "abcdefgh",        # 8 chars, prefix なし
            "playbook",        # 8 chars, prefix なし
            "worker01",        # 8 chars, prefix なし
            "designer-1",      # 10 chars, prefix なし
            "w",               # 1 char, prefix `w-` を満たさない
            "W-Playbook",      # 大文字 prefix (`W-` は `w-` ではない)
            "w_playbook",      # `w_` は `w-` で始まらない (アンダースコア)
            "-playbook",       # 先頭ハイフン (prefix なし)
            "1playbook",       # 数字始まり (prefix なし)
        ],
    )
    def test_missing_prefix_rejected_with_recommendation(self, alias: str):
        err = ow_service._validate_alias_format(alias)
        assert err is not None
        assert "must start with 'w-'" in err
        # Friendly Error: 推奨命名規約がメッセージに含まれる
        assert "Recommended" in err

    @pytest.mark.parametrize(
        "alias",
        [
            "w-Playbook",      # prefix OK, 長さ OK, 大文字混入
            "w-play book",     # スペース
            "w-play.book",     # ドット
            "w-プレイブック",   # 非ASCII
            "w--playbook",     # 連続ハイフン
            "w-1playbook",     # prefix 後の先頭が数字
            "w-playbook-",     # prefix 後の末尾がハイフン
        ],
    )
    def test_invalid_kebab_case_rejected(self, alias: str):
        err = ow_service._validate_alias_format(alias)
        assert err is not None
        assert "kebab-case" in err

    def test_empty_alias_rejected(self):
        err = ow_service._validate_alias_format("")
        assert err is not None
        assert "non-empty" in err

    def test_non_string_rejected(self):
        # 型ガードも兼ねる
        assert ow_service._validate_alias_format(None) is not None  # type: ignore[arg-type]


# ----------------------------
# _suggest_alias: 書式違反 alias から修正候補組み立て
# ----------------------------


class TestSuggestAlias:
    """書式違反 alias → 修正候補のテスト"""

    def test_too_short_with_activity_id_builds_suffix(self):
        # `w-p26` (5字) + activity_id=1064 → `w-p26-1064` (10字、validation 通過)
        assert ow_service._suggest_alias("w-p26", 1064, 1) == "w-p26-1064"

    def test_missing_prefix_with_activity_id_adds_prefix_and_suffix(self):
        # `pp` (prefix なし) + activity_id=1064 → `w-pp-1064` (9字、validation 通過)
        assert ow_service._suggest_alias("pp", 1064, 1) == "w-pp-1064"

    def test_freeform_name_with_activity_id(self):
        # `anvil` + activity_id=1064 → `w-anvil-1064`
        assert ow_service._suggest_alias("anvil", 1064, 1) == "w-anvil-1064"

    def test_uppercase_lowered_and_underscore_converted(self):
        # `W_Playbook` → 小文字化 + アンダースコア→ハイフン → `w-playbook` core → suffix
        assert ow_service._suggest_alias("W_Playbook", 1064, 1) == "w-playbook-1064"

    def test_activity_id_none_falls_back_to_task_n(self):
        # activity_id 不在 → `-t<task_n>` suffix (例: `w-anvil-t5` 10字、validation 通過)
        assert ow_service._suggest_alias("anvil", None, 5) == "w-anvil-t5"

    def test_task_n_fallback_too_short_returns_none(self):
        # `pp` + task_n=5 → `w-pp-t5` (7字) で長さ違反 → None
        # core が短すぎると fallback でも救えないケース
        assert ow_service._suggest_alias("pp", None, 5) is None

    def test_no_hints_returns_none_when_too_short(self):
        # `pp` 単独で activity_id / task_n どちらもなければ `w-pp` (4字) で長さ違反 → None
        assert ow_service._suggest_alias("pp", None, None) is None

    def test_no_hints_returns_core_when_long_enough(self):
        # `crystal` (7字) → core `w-crystal` (9字) で validation 通る
        assert ow_service._suggest_alias("crystal", None, None) == "w-crystal"

    def test_empty_returns_none(self):
        assert ow_service._suggest_alias("", 1064, 1) is None

    def test_non_string_returns_none(self):
        assert ow_service._suggest_alias(None, 1064, 1) is None  # type: ignore[arg-type]


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

    def test_missing_prefix_alias_yields_warning(self, tmp_path):
        # `W-Playbook` は `w-` で始まらないため新仕様で prefix 違反として弾かれる
        result = ow_service._validate_spawn_preconditions(
            alias="W-Playbook", channel="ChAbCdEf", cwd=str(tmp_path)
        )
        assert result["ok"] is False
        assert any("must start with 'w-'" in w for w in result["warnings"])

    def test_kebab_case_violation_yields_warning(self, tmp_path):
        # prefix `w-` OK、長さ 10 OK、ただし prefix 後に大文字混入で kebab-case 違反
        result = ow_service._validate_spawn_preconditions(
            alias="w-Playbook", channel="ChAbCdEf", cwd=str(tmp_path)
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

    def test_short_alias_with_activity_id_attaches_suggested(self, tmp_path):
        """activity_id を渡せば warning に Suggested alias が添えられる。"""
        result = ow_service._validate_spawn_preconditions(
            alias="w-p26",
            channel="ChAbCdEf",
            cwd=str(tmp_path),
            activity_id=1064,
        )
        assert result["ok"] is False
        assert any("Suggested alias: w-p26-1064" in w for w in result["warnings"])

    def test_short_alias_without_activity_id_uses_task_n_fallback(self, tmp_path):
        """activity_id 不在なら task_n fallback で Suggested alias が組まれる。"""
        result = ow_service._validate_spawn_preconditions(
            alias="w-p26",
            channel="ChAbCdEf",
            cwd=str(tmp_path),
            task_n=5,
        )
        assert result["ok"] is False
        assert any("Suggested alias: w-p26-t5" in w for w in result["warnings"])

    def test_dispatcher_validator_does_not_attach_worker_suggestion(self, tmp_path):
        """dispatcher 経路では worker 用 Suggested alias を添付しない (role 取り違え防止)。"""
        result = ow_service._validate_spawn_preconditions(
            alias="x-invalid",
            channel="ChAbCdEf",
            cwd=str(tmp_path),
            alias_validator=ow_service._validate_dispatcher_handle,
            activity_id=1064,
        )
        assert result["ok"] is False
        assert not any("Suggested alias" in w for w in result["warnings"])


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
        # 明示的に "manual" を指定してアダプタ呼び出しを抑止する
        # (デフォルトの "tmux" だと実環境の tmux 経路が走り得るためテスト分離が崩れる)
        monkeypatch.setenv("OW_TERMINAL", "manual")

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

    def test_missing_prefix_alias_returns_precondition_failed(self, monkeypatch, tmp_path):
        # `W-Playbook` は新仕様で prefix `w-` 違反として SPAWN_PRECONDITION_FAILED
        result = ow_service.ow_spawn_worker(
            alias="W-Playbook",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="d", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "SPAWN_PRECONDITION_FAILED"
        assert any("must start with 'w-'" in w for w in result["error"]["warnings"])

    def test_underscore_alias_returns_precondition_failed(self, monkeypatch, tmp_path):
        # `w_playbook` は `w_` で始まり `w-` で始まらないため prefix 違反として弾かれる
        result = ow_service.ow_spawn_worker(
            alias="w_playbook",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="d", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "SPAWN_PRECONDITION_FAILED"

    def test_kebab_case_alias_returns_precondition_failed(self, monkeypatch, tmp_path):
        # prefix OK、長さ OK、kebab-case 違反 (大文字混入)
        result = ow_service.ow_spawn_worker(
            alias="w-Playbook",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="d", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "SPAWN_PRECONDITION_FAILED"
        assert any("kebab-case" in w for w in result["error"]["warnings"])

    def test_short_alias_with_activity_id_includes_suggested(self, monkeypatch, tmp_path):
        # activity_id 渡し時は SPAWN_PRECONDITION_FAILED の warnings に Suggested が乗る
        result = ow_service.ow_spawn_worker(
            alias="w-p26",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="d", task_n=1,
            activity_id=1064,
        )
        assert "error" in result
        assert any(
            "Suggested alias: w-p26-1064" in w
            for w in result["error"]["warnings"]
        )

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
        # 明示的に "manual" を指定してアダプタ呼び出しを抑止する
        # (デフォルトの "tmux" だと実環境の tmux 経路が走り得るためテスト分離が崩れる)
        monkeypatch.setenv("OW_TERMINAL", "manual")

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
        # 明示的に "manual" を指定してアダプタ呼び出しを抑止する
        # (デフォルトの "tmux" だと実環境の tmux 経路が走り得るためテスト分離が崩れる)
        monkeypatch.setenv("OW_TERMINAL", "manual")

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
