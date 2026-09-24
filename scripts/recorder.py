"""記録役セッションの起動・停止・状態確認CLIエントリポイント (start/stop/status)

実装本体は src/services/recorder_launcher_service.py に置く。
"""
import sys
from pathlib import Path

# プロジェクトルートをパスに追加（src.services等の参照用）
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.services.recorder_launcher_service import main  # noqa: E402

if __name__ == "__main__":
    main()
