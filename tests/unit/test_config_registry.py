"""環境変数の台帳（src/config_registry.py）の整合テスト。

台帳がコード・ドキュメントの実態とずれないことを検証する。
"""
import re
from pathlib import Path

import pytest

from src import config_registry
from src.config_registry import ENV_VARS, KINDS

_ROOT = Path(__file__).resolve().parent.parent.parent
_NAMES = {v.name for v in ENV_VARS}
_USER_NAMES = {v.name for v in ENV_VARS if v.kind == "user"}


def _code_env_names() -> set[str]:
    found: set[str] = set()
    for sub in ("src", "hooks", "scripts"):
        for path in (_ROOT / sub).rglob("*.py"):
            if path.name == "config_registry.py":
                continue
            found |= set(re.findall(r'"(CALM_[A-Z0-9_]+)"', path.read_text()))
    return found


def test_registry_has_no_duplicates_and_valid_kinds():
    assert len(_NAMES) == len(ENV_VARS)
    assert all(v.kind in KINDS for v in ENV_VARS)


def test_every_env_var_read_in_code_is_registered():
    assert _code_env_names() - _NAMES == set()


def test_registered_names_are_read_somewhere_in_code():
    # CALM_RECENCY_DECAY_FLOOR のような接頭辞一致を拾わないよう、完全一致の文字列だけ見る
    assert _NAMES - _code_env_names() == set()


def test_defaults_match_config_py():
    src = (_ROOT / "src" / "config.py").read_text()
    pairs = re.findall(r'env_get\(\s*"(CALM_[A-Z0-9_]+)"\s*,\s*"([^"]*)"', src)
    assert pairs
    by_name = {v.name: v for v in ENV_VARS}
    mismatched = [(n, d, by_name[n].default) for n, d in pairs if by_name[n].default != d]
    assert mismatched == []


@pytest.mark.parametrize("doc", ["docs/setup.md", "skills/man/SKILL.md"])
def test_doc_env_table_matches_user_entries(doc):
    text = (_ROOT / doc).read_text()
    in_table = set(re.findall(r"^\| `(CALM_[A-Z0-9_]+)` \|", text, re.MULTILINE))
    assert in_table == _USER_NAMES


def test_get_config_env_vars_default_is_user_only(temp_db, monkeypatch):
    from src.main import get_config

    monkeypatch.setenv("CALM_HEARTBEAT_TIMEOUT", "45")
    rows = {r["name"]: r for r in get_config()["env_vars"]}
    assert set(rows) == _USER_NAMES
    assert rows["CALM_HEARTBEAT_TIMEOUT"]["value"] == "45"


def test_get_config_env_kind_all_returns_every_entry(temp_db):
    from src.main import get_config

    assert {r["name"] for r in get_config(env_kind="all")["env_vars"]} == _NAMES


def test_list_env_vars_reads_legacy_prefix(monkeypatch):
    monkeypatch.delenv("CALM_SNAPSHOT_MAX_COUNT", raising=False)
    monkeypatch.setenv("CCM_SNAPSHOT_MAX_COUNT", "9")
    rows = {r["name"]: r for r in config_registry.list_env_vars("user")}
    assert rows["CALM_SNAPSHOT_MAX_COUNT"]["value"] == "9"


def test_removed_display_limit_vars_are_gone():
    assert "CALM_IN_PROGRESS_LIMIT" not in _NAMES
    assert "CALM_PENDING_LIMIT" not in _NAMES
