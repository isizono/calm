"""src/infra/plugin_install.py のユニットテスト。

installed_plugins.jsonの読み取り・プラグインキー導出・インストール先解決の
各分岐（正常・ファイル無し・パース不能・エントリ無し・パス不存在・
キー導出不能）を検証する。
"""
import json

import pytest

from src.infra import plugin_install


def _write_installed_plugins(path, key: str, install_path: str, *, scope: str = "user"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 2,
        "plugins": {
            key: [
                {"scope": scope, "installPath": install_path, "version": "deadbeef"},
            ]
        },
    }), encoding="utf-8")


def _bundled_root(tmp_path, *, marketplace="calm-marketplace", plugin="calm", version="abc123"):
    root = tmp_path / "plugins" / "cache" / marketplace / plugin / version
    root.mkdir(parents=True)
    return root


def test_resolve_installed_plugin_root_returns_install_path_when_entry_matches(tmp_path, monkeypatch):
    bundled = _bundled_root(tmp_path)
    installed_dir = tmp_path / "installed" / "newver"
    installed_dir.mkdir(parents=True)
    plugins_json = tmp_path / "installed_plugins.json"
    _write_installed_plugins(plugins_json, "calm@calm-marketplace", str(installed_dir))
    monkeypatch.setenv(plugin_install.INSTALLED_PLUGINS_PATH_ENV, str(plugins_json))

    result = plugin_install.resolve_installed_plugin_root(bundled)

    assert result == installed_dir


def test_resolve_installed_plugin_root_none_when_file_missing(tmp_path, monkeypatch):
    bundled = _bundled_root(tmp_path)
    monkeypatch.setenv(plugin_install.INSTALLED_PLUGINS_PATH_ENV, str(tmp_path / "does_not_exist.json"))

    assert plugin_install.resolve_installed_plugin_root(bundled) is None


def test_resolve_installed_plugin_root_none_when_install_path_does_not_exist(tmp_path, monkeypatch):
    bundled = _bundled_root(tmp_path)
    plugins_json = tmp_path / "installed_plugins.json"
    _write_installed_plugins(plugins_json, "calm@calm-marketplace", str(tmp_path / "ghost_version"))
    monkeypatch.setenv(plugin_install.INSTALLED_PLUGINS_PATH_ENV, str(plugins_json))

    assert plugin_install.resolve_installed_plugin_root(bundled) is None


def test_resolve_installed_plugin_root_none_when_entry_key_missing(tmp_path, monkeypatch):
    bundled = _bundled_root(tmp_path)
    plugins_json = tmp_path / "installed_plugins.json"
    _write_installed_plugins(plugins_json, "other-plugin@other-marketplace", str(tmp_path))
    monkeypatch.setenv(plugin_install.INSTALLED_PLUGINS_PATH_ENV, str(plugins_json))

    assert plugin_install.resolve_installed_plugin_root(bundled) is None


def test_resolve_installed_plugin_root_none_when_json_malformed(tmp_path, monkeypatch):
    bundled = _bundled_root(tmp_path)
    plugins_json = tmp_path / "installed_plugins.json"
    plugins_json.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv(plugin_install.INSTALLED_PLUGINS_PATH_ENV, str(plugins_json))

    assert plugin_install.resolve_installed_plugin_root(bundled) is None


def test_resolve_installed_plugin_root_none_when_bundled_root_not_under_plugin_cache_layout(tmp_path, monkeypatch):
    """gitチェックアウトからの直接実行等、プラグインキャッシュの配置規則に
    合致しないbundled_rootはキー導出不能としてNoneを返す(フォールバック対象)。"""
    bundled = tmp_path / "workspace" / "calm"
    bundled.mkdir(parents=True)
    plugins_json = tmp_path / "installed_plugins.json"
    _write_installed_plugins(plugins_json, "calm@calm-marketplace", str(tmp_path))
    monkeypatch.setenv(plugin_install.INSTALLED_PLUGINS_PATH_ENV, str(plugins_json))

    assert plugin_install.resolve_installed_plugin_root(bundled) is None


def test_resolve_installed_plugin_root_prefers_user_scope_entry(tmp_path, monkeypatch):
    bundled = _bundled_root(tmp_path)
    user_dir = tmp_path / "user_install"
    user_dir.mkdir()
    project_dir = tmp_path / "project_install"
    project_dir.mkdir()
    plugins_json = tmp_path / "installed_plugins.json"
    plugins_json.write_text(json.dumps({
        "version": 2,
        "plugins": {
            "calm@calm-marketplace": [
                {"scope": "project", "installPath": str(project_dir)},
                {"scope": "user", "installPath": str(user_dir)},
            ]
        },
    }), encoding="utf-8")
    monkeypatch.setenv(plugin_install.INSTALLED_PLUGINS_PATH_ENV, str(plugins_json))

    assert plugin_install.resolve_installed_plugin_root(bundled) == user_dir


def test_installed_plugins_path_defaults_under_home(monkeypatch):
    monkeypatch.delenv(plugin_install.INSTALLED_PLUGINS_PATH_ENV, raising=False)

    path = plugin_install.installed_plugins_path()

    assert path.name == "installed_plugins.json"
    assert path.parts[-3:] == (".claude", "plugins", "installed_plugins.json")
