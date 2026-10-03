"""Windowsの共有違反（WinError 32）を吸収する、ファイル置換の薄いラッパー。

別プロセスがファイルを開いている瞬間に`os.replace`を行うと、Windowsでは
PermissionError（WinError 32）になりうる（POSIXはopen中のファイルでも
renameできるため起きない）。多くは一瞬の競合なので、短い間隔で数回
再試行すれば解消する。
"""
import os
import time

_RETRY_ATTEMPTS = 5
_RETRY_DELAY_SEC = 0.05


def replace_retrying(src: "os.PathLike[str] | str", dst: "os.PathLike[str] | str") -> None:
    """os.replaceを短く再試行する。最終試行の失敗はそのままraiseする。"""
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if attempt == _RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_RETRY_DELAY_SEC)
