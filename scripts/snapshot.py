"""DBスナップショットCLIエントリポイント

実装本体は src/services/backup_service.py に移設した。
本モジュールは `uv run python scripts/snapshot.py <command>` 経由の起動と、
既存コードとの後方互換のためのre-exportのみを持つ。
"""
import sys
from pathlib import Path

# プロジェクトルートをパスに追加（src.config等の参照用）
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.services.backup_service import (  # noqa: F401  (後方互換のためのre-export)
    HEALTH_CHECK_TABLES,
    KIND_QUOTAS,
    SNAPSHOT_DB_SUFFIX,
    SNAPSHOT_JSON_SUFFIX,
    SNAPSHOT_PREFIX,
    HealthCheckResult,
    RestoreBlockedError,
    RestoreResult,
    SnapshotKind,
    get_row_counts,
    health_check,
    list_snapshots,
    main,
    restore_snapshot,
    should_take_snapshot,
    snapshot_dir_for,
    take_snapshot,
    verify_snapshot,
)

if __name__ == "__main__":
    main()
