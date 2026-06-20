"""crash 復旧テストは queue 撤廃 (D#2791) に伴い旧 queue.md 前提のものを削除した。

cache.workers ベースの新 detect_crash_inconsistencies / ow_recover 経路は
projector wire-in 後に新規テストで網羅する想定 (D#2750)。本 PR では一旦削除のみで
新規テストは追加しない。
"""

import pytest


@pytest.mark.skip(
    reason=(
        "queue.md 前提の crash recovery テストを撤廃した。新真実源モデル "
        "(cache.workers ベース) の detect_crash_inconsistencies / ow_recover 経路は "
        "projector wire-in 完了後に書き下ろす (別 [作業] activity)"
    )
)
def test_placeholder_for_projector_wire_in_tests() -> None:
    """次フェーズで cache 由来 crash recovery テストに置き換える placeholder。"""
