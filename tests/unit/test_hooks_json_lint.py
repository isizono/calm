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
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_DIR = _PROJECT_ROOT / "hooks"
_HOOKS_JSON_PATH = _HOOKS_DIR / "hooks.json"

# hooks/ 配下のスクリプトが自身の担当イベントを宣言する規約:
# モジュール docstring 冒頭が `"<Event> hook: ..."` の形。
_DECLARED_EVENT_PATTERN = re.compile(r"^(\w+) hook:")
_COMMAND_SCRIPT_PATTERN = re.compile(r"hooks/(\S+\.py)")


def _load_hooks_json() -> dict:
    return json.loads(_HOOKS_JSON_PATH.read_text(encoding="utf-8"))


def _registered_scripts_by_event() -> dict[str, list[str]]:
    """hooks.json の各イベントに登録されているスクリプトのファイル名一覧を返す。"""
    data = _load_hooks_json()
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
