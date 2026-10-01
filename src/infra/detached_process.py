"""子プロセスを起動元から切り離すための共通ヘルパー。

POSIXでは`start_new_session=True`でセッション・コンソールから切り離す。
Windowsでは同じ効果を`creationflags`で得る必要があり、`start_new_session`は
Windows版のPopenでは受け取られるだけで無視される。CREATE_NEW_PROCESS_GROUPで
新しいプロセスグループに入れてCtrl+Cの伝播を断ち、CREATE_NO_WINDOWで
コンソール窓を抑止する。DETACHED_PROCESSは使わない
（孫プロセスが新規コンソールを開くため）。stdinは閉じた端末・pipeの中身が
意図せず流れ込まないようDEVNULLにする。

CREATE_NEW_PROCESS_GROUP/CREATE_NO_WINDOWはWindows版のsubprocessモジュールに
しか定義されないため、属性参照ではなく値を直接定数化する
（他OS上でもこの関数のWindows分岐を単体テストできるようにするため）。
"""
from __future__ import annotations

import subprocess
import sys

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def popen_detached(args, *, cwd=None, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) -> subprocess.Popen:
    """親から切り離した子プロセスを起動する。"""
    kwargs: dict = {"cwd": cwd, "stdout": stdout, "stderr": stderr}
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)
