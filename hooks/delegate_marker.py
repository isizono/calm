"""委譲先セッションの目印ファイル(marker)管理。

scripts/bg_dispatch.pyで依頼文を作った宛先activityに目印を置く。Stop hookの
記録義務blockは、check-inしているactivityにこの目印があるセッション(=依頼文で
立てた委譲先)に限って発火する。

委譲先のsession_idは依頼文の生成時点では分からないため、記録役の目印
(hooks.recorder_marker)と違いactivity_idをキーにする。置き場は同じ
HookState.BASE_DIR配下。標準ライブラリのみに依存する。
"""
import os
import time
from pathlib import Path

from hooks.hook_state import HookState

# 目印がこの秒数より古ければ委譲先ではないとみなす。委譲先の作業が終わったあと
# 同じactivityへ窓口がcheck-inしてもblockされないようにするための寿命。
# ponytail: 近似。TTL内に窓口が同じactivityへcheck-inすれば1回blockされ得るし、24hを超える
# 委譲先作業はblock対象から外れる。厳密にするなら委譲先のcheck-in時にsession_idへ紐付ける。
_MARKER_TTL_SEC = 24 * 60 * 60


def marker_path(activity_id: int) -> Path:
    # stop_hookと同じくHOOK_STATE_DIRがあればそちらを優先する(グローバルは書き換えない)
    base = Path(os.environ["HOOK_STATE_DIR"]) if os.environ.get("HOOK_STATE_DIR") else HookState.BASE_DIR
    return base / "delegate" / f"{int(activity_id)}"


def write_delegate_marker(activity_id: int) -> None:
    """目印を書く(mtimeが寿命の起点)。HOOK_STATE_DIRがあればhookと同じ場所へ書く。

    目印は補助情報なので、書けなくても例外は外に出さない(依頼文の出力を止めない)。
    """
    try:
        path = marker_path(activity_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1", encoding="utf-8")
    except OSError:
        pass


def is_delegate_activity(activity_id: int | None) -> bool:
    """activity_idに委譲先の目印があるか。取れない場合は「無い」扱い(blockしない側)。"""
    if activity_id is None:
        return False
    try:
        age_sec = time.time() - marker_path(activity_id).stat().st_mtime
        return age_sec <= _MARKER_TTL_SEC
    except OSError:
        return False
