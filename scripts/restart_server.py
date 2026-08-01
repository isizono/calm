"""MCPサーバー強制再起動CLIエントリポイント

実装本体は src/services/restart_service.py に置く。
本モジュールは `uv run python scripts/restart_server.py` 経由の起動のみを持つ。
"""
import sys
from pathlib import Path

# プロジェクトルートをパスに追加（src.services等の参照用）
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.services.restart_service import main  # noqa: E402

if __name__ == "__main__":
    main()
