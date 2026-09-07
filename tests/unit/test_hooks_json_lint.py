"""hooks/hooks.json の配線を実装から導出した期待値と突き合わせる lint テスト。

「ロジックは正しいが hooks.json に未登録で発火しない」という配線バグ
(sanitize_tool_result_hook.py / sanitize_backfill_hook.py が該当) は、フック
関数自体の単体テストでは検出できない。hooks.json は文書ではなく Claude Code
harness が読むランタイム契約そのものであるため、実装 (各フックスクリプトの
モジュール docstring 冒頭 `"<Event> hook: ..."`) から期待される登録イベントを
機械的に導出し、hooks.json の実際の登録内容と突き合わせる。
"""
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_DIR = _PROJECT_ROOT / "hooks"
_HOOKS_JSON_PATH = _HOOKS_DIR / "hooks.json"
_CODEX_HOOKS_JSON_PATH = _PROJECT_ROOT / ".codex" / "hooks.json"

# Codex CLIのhookイベント一覧（codex-rs/hooks/src/lib.rs HOOK_EVENT_NAMES）に
# 存在しないイベント。Codex側登録（.codex/hooks.json）の期待値導出から除外する。
_CODEX_UNSUPPORTED_EVENTS = {"MessageDisplay"}

# イベント自体はCodexに存在するが、スクリプトが依存するハーネス機構が
# Codexに無いため、Codex側登録の期待値導出から除外するスクリプト。
# relay_monitor_watch_hook.py: matcher ^Monitor$ が対象とするMonitorツール
# （イベント駆動の永続監視）がCodexに存在せず、登録しても発火し得ない（#616）。
_CODEX_UNSUPPORTED_SCRIPTS = {"relay_monitor_watch_hook.py"}

# hooks/ 配下のスクリプトが自身の担当イベントを宣言する規約:
# モジュール docstring 冒頭が `"<Event> hook: ..."` の形。
_DECLARED_EVENT_PATTERN = re.compile(r"^(\w+) hook:")
_COMMAND_SCRIPT_PATTERN = re.compile(r"hooks/(\S+\.py)")


def _load_hooks_json() -> dict:
    return json.loads(_HOOKS_JSON_PATH.read_text(encoding="utf-8"))


def _registered_scripts_by_event(hooks_json_path: Path = _HOOKS_JSON_PATH) -> dict[str, list[str]]:
    """hooks.json の各イベントに登録されているスクリプトのファイル名一覧を返す。"""
    data = json.loads(hooks_json_path.read_text(encoding="utf-8"))
    result: dict[str, list[str]] = {}
    for event_name, matcher_blocks in data.get("hooks", {}).items():
        scripts: list[str] = []
        for block in matcher_blocks:
            for entry in block.get("hooks", []):
                command = entry.get("command", "")
                scripts.extend(_COMMAND_SCRIPT_PATTERN.findall(command))
        result[event_name] = scripts
    return result


def _declared_event(script_path: Path) -> str | None:
    """script_path の docstring 冒頭が宣言する担当イベント名を返す。

    規約に従わないヘルパーモジュール (hooks.json に直接登録される想定でない
    もの) は None を返す。
    """
    doc = ast.get_docstring(ast.parse(script_path.read_text(encoding="utf-8")))
    if not doc:
        return None
    m = _DECLARED_EVENT_PATTERN.match(doc)
    return m.group(1) if m else None


def _declaring_hook_scripts() -> list[Path]:
    scripts = []
    for path in sorted(_HOOKS_DIR.glob("*.py")):
        if _declared_event(path) is not None:
            scripts.append(path)
    return scripts


_DECLARING_SCRIPTS = _declaring_hook_scripts()


class TestHooksJsonScriptExistence:
    """hooks.json が参照するコマンドが実在スクリプトを指すことの構造smoke。"""

    def test_all_referenced_scripts_exist_on_disk(self):
        registered = _registered_scripts_by_event()
        missing = [
            script
            for scripts in registered.values()
            for script in scripts
            if not (_HOOKS_DIR / script).exists()
        ]
        assert missing == []


@pytest.mark.parametrize(
    "script_path", _DECLARING_SCRIPTS, ids=[p.name for p in _DECLARING_SCRIPTS]
)
def test_declared_event_script_is_registered_in_hooks_json(script_path: Path):
    """script_path が docstring で宣言する hook イベントに、hooks.json 上で
    実際に登録されていることを確認する。

    ロジックは正しいのに hooks.json に未登録、という配線バグ (今回の
    sanitize_tool_result_hook.py / sanitize_backfill_hook.py のケース) を、
    今後別のフックが同種の欠陥を持った場合も含めて検知する。
    """
    event = _declared_event(script_path)
    registered = _registered_scripts_by_event()
    assert script_path.name in registered.get(event, [])


class TestCodexHooksJsonConsistency:
    """.codex/hooks.json (Codex CLI向けのプロジェクト層hook登録) の整合性lint。

    Codex側の登録はClaude Code側 (hooks/hooks.json) と同じ配線契約であり、
    「スクリプトは正しいが未登録で発火しない」という同種の配線バグを持ちうる。
    期待値はClaude Code側の登録内容からCodexに存在しないイベントを除外して
    機械的に導出し、両ファイルの登録が乖離したら検知する。
    """

    def test_all_referenced_scripts_exist_on_disk(self):
        registered = _registered_scripts_by_event(_CODEX_HOOKS_JSON_PATH)
        missing = [
            script
            for scripts in registered.values()
            for script in scripts
            if not (_HOOKS_DIR / script).exists()
        ]
        assert missing == []

    def test_registration_matches_claude_code_minus_unsupported_events(self):
        """イベントごとの登録スクリプト列 (順序含む) がClaude Code側と一致する。

        順序も比較対象に含む: SessionStartはhook_state.py clearが先頭で
        走らないと、前セッションのstateを引き継いだまま各hookが動く。
        """
        claude = _registered_scripts_by_event()
        codex = _registered_scripts_by_event(_CODEX_HOOKS_JSON_PATH)
        expected = {
            event: [s for s in scripts if s not in _CODEX_UNSUPPORTED_SCRIPTS]
            for event, scripts in claude.items()
            if event not in _CODEX_UNSUPPORTED_EVENTS
        }
        assert codex == expected


@pytest.mark.parametrize(
    "script_path", _DECLARING_SCRIPTS, ids=[p.name for p in _DECLARING_SCRIPTS]
)
def test_script_module_imports_succeed_under_declared_cwd(script_path: Path):
    """hooks.json の実行コマンド (`cd ${CLAUDE_PLUGIN_ROOT} && uv run python
    hooks/<script>.py`) が想定する cwd = プロジェクトルートで、スクリプトの
    トップレベル import が解決できることを確認する。

    hooks/*.py は `hooks.xxx` / `src.xxx` の絶対 import を使う規約のため、
    プロジェクトルートを sys.path に追加する処理 (`sys.path.insert(0, ...)`)
    を各スクリプト自身が持たないと `ModuleNotFoundError` で起動時に落ちる。
    この種の欠陥は単体テスト (pytest 実行時は既にプロジェクトルートが
    sys.path 上にある) では検出できず、実際に別プロセスとして起動する形で
    しか再現しない。
    """
    relative_path = script_path.relative_to(_PROJECT_ROOT)
    # `python -c` は sys.path[0] が '' (cwd) になり、hooks.json が実際に叩く
    # `python hooks/<script>.py` (sys.path[0] = スクリプトの親ディレクトリ) と
    # sys.path の状態が異なる。sys.path[0] を明示的にスクリプトの親ディレクトリへ
    # 上書きしてから run_path することで、本番と同じ import 解決条件を再現する。
    setup = f"import sys; sys.path[0] = {str(script_path.parent)!r}"
    run = f"import runpy; runpy.run_path({str(relative_path)!r}, run_name='_hooks_json_lint_import_check')"
    result = subprocess.run(
        [sys.executable, "-c", f"{setup}\n{run}"],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"{relative_path} のトップレベル import が cwd={_PROJECT_ROOT} で失敗した:\n"
        f"{result.stderr}"
    )
