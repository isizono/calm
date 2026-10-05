"""委譲先セッションの目印ファイル(marker)管理。

scripts/bg_dispatch.pyで依頼文を作った宛先activityに目印を置く。Stop hookの
記録義務blockは、check-inしているactivityにこの目印があるセッション(=依頼文で
立てた委譲先)に限って発火する。

委譲先のsession_idは依頼文の生成時点では分からないため、記録役の目印
(hooks.recorder_marker)と違いactivity_idをキーにする。置き場は同じ
HookState.BASE_DIR配下。標準ライブラリのみに依存する。
"""
from pathlib import Path

from hooks.hook_state import HookState


def marker_path(activity_id: int) -> Path:
    return HookState.BASE_DIR / "delegate" / f"{int(activity_id)}"


def write_delegate_marker(activity_id: int) -> None:
    path = marker_path(activity_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1", encoding="utf-8")


def is_delegate_activity(activity_id: int | None) -> bool:
    """activity_idに委譲先の目印があるか。取れない場合は「無い」扱い(blockしない側)。"""
    if activity_id is None:
        return False
    try:
        return marker_path(activity_id).exists()
    except OSError:
        return False
