"""Claude Codeが現在有効としているプラグインのインストール先を解決する。

プラグインキャッシュ配置では、自分がバンドルされているディレクトリ
（`.../plugins/cache/<marketplace>/<plugin>/<version>/`）は起動時点のスナップ
ショットであり、`claude plugin update`等で別バージョンに切り替わっていても
そのプロセスの生存中は古いバージョンを指し続ける。本モジュールは
`~/.claude/plugins/installed_plugins.json`を都度読み直すことで、呼び出し時点の
「現在有効なバージョン」を得る手段を提供する。
"""
from __future__ import annotations

import json
from pathlib import Path

from src.env_compat import env_get

INSTALLED_PLUGINS_PATH_ENV = "CALM_INSTALLED_PLUGINS_PATH"


def installed_plugins_path() -> Path:
    """`installed_plugins.json`のパス（既定 `~/.claude/plugins/installed_plugins.json`）。"""
    raw = env_get(INSTALLED_PLUGINS_PATH_ENV)
    return Path(raw).expanduser() if raw else Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def _plugin_key(bundled_root: Path) -> str | None:
    """`bundled_root`から`installed_plugins.json`のキー（`<plugin>@<marketplace>`）を導出する。

    プラグインキャッシュの配置規則`.../plugins/cache/<marketplace>/<plugin>/<version>/`を
    前提に、bundled_root自身をversionディレクトリとみなして2階層上をたどる。
    この規則に合致しない場合（gitチェックアウトからの直接実行等）はNoneを返す
    （呼び出し側はバンドル自身へフォールバックする）。
    """
    version_dir = bundled_root.resolve()
    plugin_dir = version_dir.parent
    marketplace_dir = plugin_dir.parent
    cache_dir = marketplace_dir.parent
    if cache_dir.name != "cache" or cache_dir.parent.name != "plugins":
        return None
    return f"{plugin_dir.name}@{marketplace_dir.name}"


def resolve_installed_plugin_root(bundled_root: Path) -> Path | None:
    """`bundled_root`から導出したプラグインエントリの、現在のインストール先を返す。

    解決できない場合（キー導出不能・ファイル欠落・パース失敗・エントリ無し・
    installPathが存在しない）はNoneを返す。エントリが複数（マルチスコープ）の
    場合はscope=="user"を優先し、無ければ先頭を使う。
    """
    key = _plugin_key(bundled_root)
    if key is None:
        return None

    try:
        data = json.loads(installed_plugins_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    entries = data.get("plugins", {}).get(key)
    if not isinstance(entries, list) or not entries:
        return None

    entry = next((e for e in entries if isinstance(e, dict) and e.get("scope") == "user"), entries[0])
    if not isinstance(entry, dict):
        return None
    install_path = entry.get("installPath")
    if not isinstance(install_path, str) or not install_path:
        return None

    resolved = Path(install_path)
    return resolved if resolved.is_dir() else None
