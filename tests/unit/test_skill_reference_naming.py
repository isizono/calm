"""AI向け案内文中のスキル参照（`<plugin-name>:<skill-name>`）の整合性を検証する
導出型整合性lint。

期待値は2つの実装から導出する（ハードコードしない）。

- 現行プラグイン名: `.claude-plugin/plugin.json` の `name`
- このプラグインが過去に名乗った名前の集合: `hooks/preblock_hook.py` の
  `_PROJECT_NAMES`（pyproject.toml `[project].name` の許容値として既に
  一次情報になっている）

対象ソース（`src.main.RULES` と `hooks/*.py` のソース文字列）から
`<name-in-_PROJECT_NAMES>:<skill>` 形式のトークンを正規表現で抽出し、

1. 現行プラグイン名を prefix に持つ参照は、すべて `skills/<skill>/` が
   実在すること
2. 現行プラグイン名以外（過去に名乗っていた名前）を prefix に持つ参照は
   1件も無いこと

を検証する。`_PROJECT_NAMES` が改称のたびに更新される前提のもとでは、次の
改称後もこのテストは書き換え不要で追従する
（docs/spec/test-convention.md §2 の許可2形のうち「導出型整合性lint」）。
"""
import json
import re
import sys
from pathlib import Path

import pytest

from src.main import RULES

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _REPO_ROOT / "hooks"
_SKILLS_DIR = _REPO_ROOT / "skills"

if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import preblock_hook  # type: ignore  # noqa: E402

_CURRENT_PLUGIN_NAME = json.loads(
    (_REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
)["name"]

_KNOWN_PLUGIN_NAMES = preblock_hook._PROJECT_NAMES
_LEGACY_PLUGIN_NAMES = tuple(n for n in _KNOWN_PLUGIN_NAMES if n != _CURRENT_PLUGIN_NAME)


def _sources() -> list[tuple[str, str]]:
    sources = [("src/main.py (RULES)", RULES)]
    sources.extend(
        (f"hooks/{path.name}", path.read_text(encoding="utf-8"))
        for path in sorted(_HOOKS_DIR.glob("*.py"))
    )
    return sources


def _skill_refs(text: str, plugin_name: str) -> list[str]:
    # \b は使わない: Python の re は片仮名・平仮名・漢字を \w とみなすため、
    # 「日本語の直後に空白なしで続く ASCII 参照」（例: "はcalm:man"）で
    # 左境界の \b が成立せずマッチを取りこぼす。ASCII の英数字・-・_ のみを
    # 境界外とみなす否定先読み/後読みに置き換える。
    prefix = re.escape(plugin_name)
    pattern = re.compile(rf"(?<![A-Za-z0-9_-]){prefix}:([a-z][a-z0-9-]*)(?![A-Za-z0-9_-])")
    return pattern.findall(text)


@pytest.fixture(params=_sources(), ids=lambda item: item[0])
def source(request) -> tuple[str, str]:
    return request.param


def test_current_plugin_name_is_calm():
    """以降のテストの前提（現行プラグイン名が_PROJECT_NAMESに含まれる）を明示する"""
    assert _CURRENT_PLUGIN_NAME in _KNOWN_PLUGIN_NAMES


def test_current_name_skill_references_resolve_to_existing_skill_dir(source):
    """現行プラグイン名を prefix とするスキル参照が、すべて skills/<name>/ として実在する"""
    label, text = source
    for skill_name in _skill_refs(text, _CURRENT_PLUGIN_NAME):
        assert (_SKILLS_DIR / skill_name).is_dir(), (
            f"{label} が参照する {_CURRENT_PLUGIN_NAME}:{skill_name} に対応する "
            f"skills/{skill_name}/ が存在しない"
        )


def test_no_legacy_plugin_name_skill_references_remain(source):
    """過去に名乗っていたプラグイン名を prefix とするスキル参照が残っていない"""
    label, text = source
    for legacy_name in _LEGACY_PLUGIN_NAMES:
        matches = _skill_refs(text, legacy_name)
        assert matches == [], (
            f"{label} に旧プラグイン名 {legacy_name}: を prefix とするスキル参照が"
            f"残っている: {matches}"
        )


def test_skill_reference_extraction_finds_current_name_references():
    """回帰保護: 抽出対象ソース全体で現行プラグイン名の参照が1件も見つからない状態は
    正規表現・走査対象パスの設定ミスを疑う（無言でvacuous passし続ける事故を防ぐ）
    """
    total = sum(len(_skill_refs(text, _CURRENT_PLUGIN_NAME)) for _, text in _sources())
    assert total > 0
