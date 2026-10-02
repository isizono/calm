"""sync-memoryの「聞き返しの後追い検出」ステップ番号参照の整合性lint。

skills/sync-memory/SKILL.mdの見出し番号を正本として導出し、同じステップを
言及する各ソースファイルの「ステップN」表記が追従しているかを検証する
（実装から期待値を導出する導出型整合性lint。docs/spec/test-convention.md §2）。
"""
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SYNC_MEMORY_SKILL_MD = _REPO_ROOT / "skills" / "sync-memory" / "SKILL.md"

_STEP_LABEL = "聞き返しの後追い検出"
_STEP_HEADING_RE = re.compile(rf"^### (\d+)\. {_STEP_LABEL}", re.MULTILINE)
_STEP_REF_RE = re.compile(r"ステップ(\d+)")

# このステップを言及しているファイル群。sync-memory SKILL.md側で見出し番号が
# 変わった場合、ここに列挙した各ファイルの表記も追従させる必要がある。
_REFERENCING_FILES = (
    _REPO_ROOT / "docs" / "spec" / "mcp-tools.md",
    _REPO_ROOT / "hooks" / "session_start_hook.py",
    _REPO_ROOT / "src" / "services" / "reask_detection_service.py",
    _REPO_ROOT / "src" / "main.py",
)


def _derive_step_number() -> str:
    text = SYNC_MEMORY_SKILL_MD.read_text(encoding="utf-8")
    match = _STEP_HEADING_RE.search(text)
    assert match is not None, (
        f"{SYNC_MEMORY_SKILL_MD} に「{_STEP_LABEL}」という見出しが見つからない"
    )
    return match.group(1)


def test_sync_memory_has_reask_followup_step_heading():
    # 失敗時はderiveのassertメッセージで気づける（vacuous passガード）
    assert _derive_step_number()


@pytest.mark.parametrize(
    "path", _REFERENCING_FILES, ids=lambda p: str(p.relative_to(_REPO_ROOT))
)
def test_step_reference_matches_sync_memory_heading(path):
    expected = _derive_step_number()
    text = path.read_text(encoding="utf-8")
    matches = set(_STEP_REF_RE.findall(text))
    assert matches, f"{path} にsync-memoryの「ステップN」参照が見つからない"
    assert matches == {expected}, (
        f"{path} の「ステップN」参照がsync-memory SKILL.mdの見出し番号"
        f"({expected})と食い違っている: {matches}"
    )
