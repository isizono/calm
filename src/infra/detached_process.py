"""子プロセスを起動元から切り離すための共通ヘルパー。

POSIXでは`start_new_session=True`でセッション・コンソールから切り離す。
Windowsでは同じ効果を`creationflags`で得る必要があり、`start_new_session`は
Windows版のPopenでは受け取られるだけで無視される。CREATE_NEW_PROCESS_GROUPで
新しいプロセスグループに入れてCtrl+Cの伝播を断ち、CREATE_NO_WINDOWで
コンソール窓を抑止する。DETACHED_PROCESSは使わない
（孫プロセスが新規コンソールを開くため）。stdinは閉じた端末・pipeの中身が
意図せず流れ込まないようDEVNULLにする。

Claude Codeが起動するプロセスはKILL_ON_JOB_CLOSE付きのJob Objectに
入っており、そこから抜けないとセッション終了時に道連れで終了させられる。
CREATE_BREAKAWAY_FROM_JOBでJob Objectから離脱させるが、Job側の設定次第では
CreateProcessがERROR_ACCESS_DENIED（winerror 5）で拒否されることがあるため、
その場合だけフラグ無しで起動し直す。

CREATE_NEW_PROCESS_GROUP/CREATE_NO_WINDOW/CREATE_BREAKAWAY_FROM_JOBは
Windows版のsubprocessモジュールにしか定義されないため、属性参照ではなく
値を直接定数化する（他OS上でもこの関数のWindows分岐を単体テストできるようにするため）。
"""
from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_ERROR_ACCESS_DENIED = 5


def popen_detached(args, *, cwd=None, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) -> subprocess.Popen:
    """親から切り離した子プロセスを起動する。"""
    kwargs: dict = {"cwd": cwd, "stdout": stdout, "stderr": stderr}
    if sys.platform == "win32":
        kwargs["stdin"] = subprocess.DEVNULL
        base_flags = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
        try:
            return subprocess.Popen(args, creationflags=base_flags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError as e:
            if getattr(e, "winerror", None) != _ERROR_ACCESS_DENIED:
                raise
            logger.warning(f"CREATE_BREAKAWAY_FROM_JOB rejected (winerror=5), retrying without it: {e}")
            return subprocess.Popen(args, creationflags=base_flags, **kwargs)
    kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)
