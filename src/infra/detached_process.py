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

さらにWindowsでは、Claude Code自身がMCPサーバーを止める際にPIDの親子関係を
たどってプロセスツリーごと終了させる挙動があり、Job Objectから抜けていても
子孫である限り道連れになる。そのため本命プロセスは直接起動せず、まず
中継プロセス（`sys.executable -c`で起動する使い捨てスクリプト）を起動し、
中継に本命を起動させてすぐ終了させる。本命は中継の子としてしか存在しない
瞬間で中継が消えるため、元のプロセスから見て子孫ではなくなる。戻り値は
`subprocess.Popen`ではなく本命のpidをpsutilで追跡するラッパーになる。

CREATE_NEW_PROCESS_GROUP/CREATE_NO_WINDOW/CREATE_BREAKAWAY_FROM_JOBは
Windows版のsubprocessモジュールにしか定義されないため、属性参照ではなく
値を直接定数化する（他OS上でもこの関数のWindows分岐を単体テストできるようにするため）。
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys

import psutil

logger = logging.getLogger(__name__)

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_ERROR_ACCESS_DENIED = 5
_RELAY_TIMEOUT_SEC = 10

# 中継プロセスの本体。stdinからargv/cwd/stdout/stderrの指定をJSONで受け取り、
# 本命を起動してpidを1行だけ標準出力に書いて終了する。中継自身もJob Objectから
# 抜ける必要があるため、本命起動時のcreationflags・winerror=5時の再試行は
# popen_detached側の分岐と同じ内容を持つ（別プロセスとして実行されるため
# コードの共有ができない）。sys.platform != "win32"のときはcreationflagsを
# 一切使わない素のPopenにフォールバックする（Mac上で中継スクリプト自体を
# 実プロセスとして検証できるようにするため）。
_RELAY_SCRIPT = r"""
import json
import subprocess
import sys


def _resolve_stream(spec, stdout_handle):
    kind = spec["kind"]
    if kind == "devnull":
        return subprocess.DEVNULL
    if kind == "same_as_stdout":
        return stdout_handle
    if kind == "path":
        return open(spec["path"], "ab")
    raise ValueError("unknown stream kind: " + repr(kind))


def _launch(argv, cwd, stdout, stderr):
    kwargs = dict(cwd=cwd, stdout=stdout, stderr=stderr)
    if sys.platform != "win32":
        return subprocess.Popen(argv, **kwargs)
    kwargs["stdin"] = subprocess.DEVNULL
    flags = 0x00000200 | 0x08000000
    try:
        return subprocess.Popen(argv, creationflags=flags | 0x01000000, **kwargs)
    except OSError as e:
        if getattr(e, "winerror", None) != 5:
            raise
        return subprocess.Popen(argv, creationflags=flags, **kwargs)


def main():
    spec = json.loads(sys.stdin.read())
    stdout_handle = _resolve_stream(spec["stdout"], None)
    stderr_handle = _resolve_stream(spec["stderr"], stdout_handle)
    proc = _launch(spec["argv"], spec.get("cwd"), stdout_handle, stderr_handle)
    sys.stdout.write(str(proc.pid) + "\n")
    sys.stdout.flush()


main()
"""


def _stream_spec(stream, *, stdout_stream=None):
    """popen_detachedに渡されたstdout/stderrを中継へ渡せるJSON形に変換する。

    ファイルオブジェクトはプロセス境界を越えて渡せないため、中継側で
    開き直せる`.name`（パス）だけを渡す。stdout/stderrに同じファイル
    オブジェクトが渡された場合（例: `stdout=stderr=log_file`）は、中継側で
    パスを2回別々に開くと書き込み位置がずれて内容が壊れうるため、
    「stdoutと同じハンドルを使え」という指示に変換する。
    """
    if stream == subprocess.DEVNULL:
        return {"kind": "devnull"}
    if stdout_stream is not None and stream is stdout_stream:
        return {"kind": "same_as_stdout"}
    path = getattr(stream, "name", None)
    if isinstance(path, str):
        return {"kind": "path", "path": path}
    raise ValueError(
        f"popen_detached: unsupported stream on Windows (DEVNULL or an open file required): {stream!r}"
    )


def _spawn_relay() -> subprocess.Popen:
    base_flags = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    relay_args = [sys.executable, "-c", _RELAY_SCRIPT]
    relay_kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        return subprocess.Popen(relay_args, creationflags=base_flags | _CREATE_BREAKAWAY_FROM_JOB, **relay_kwargs)
    except OSError as e:
        if getattr(e, "winerror", None) != _ERROR_ACCESS_DENIED:
            raise
        logger.warning(f"CREATE_BREAKAWAY_FROM_JOB rejected for relay (winerror=5), retrying without it: {e}")
        return subprocess.Popen(relay_args, creationflags=base_flags, **relay_kwargs)


class _RelayedProcess:
    """中継プロセスが起動した本命プロセスを指すハンドル。

    本命は起動直後に中継が終了するためこのプロセスの子孫ではなくなり、
    `subprocess.Popen`では追跡できない。psutilで同じpidを追跡し、
    呼び出し元が実際に使っている`subprocess.Popen`相当のインターフェース
    （pid/poll/wait/returncode/terminate/kill）だけを実装する。

    ponytail: psutilがWAIT_ABANDONED等で終了コードを特定できずNoneを返す
    ケースでは、poll()は「実行中でまだNone」と区別できない。Windows実機の
    稀なケースでしか再現せず現状は未対応（上限）。
    """

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None
        self._finished = False
        try:
            self._handle: psutil.Process | None = psutil.Process(pid)
        except psutil.NoSuchProcess:
            self._handle = None
            self._finished = True

    def poll(self) -> int | None:
        if self._finished:
            return self.returncode
        try:
            self.returncode = self._handle.wait(timeout=0)
        except psutil.TimeoutExpired:
            return None
        except psutil.NoSuchProcess:
            pass
        self._finished = True
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        if self._finished:
            return self.returncode
        try:
            self.returncode = self._handle.wait(timeout=timeout)
        except psutil.TimeoutExpired as e:
            raise subprocess.TimeoutExpired(str(self.pid), timeout) from e
        except psutil.NoSuchProcess:
            pass
        self._finished = True
        return self.returncode

    def terminate(self) -> None:
        if self._handle is not None:
            try:
                self._handle.terminate()
            except psutil.NoSuchProcess:
                pass

    def kill(self) -> None:
        if self._handle is not None:
            try:
                self._handle.kill()
            except psutil.NoSuchProcess:
                pass


def _popen_detached_windows(args, cwd, stdout, stderr) -> _RelayedProcess:
    stdout_spec = _stream_spec(stdout)
    stderr_spec = _stream_spec(stderr, stdout_stream=stdout)
    payload = json.dumps({"argv": list(args), "cwd": cwd, "stdout": stdout_spec, "stderr": stderr_spec})

    relay = _spawn_relay()
    try:
        out, err = relay.communicate(payload.encode("utf-8"), timeout=_RELAY_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        relay.kill()
        _, err = relay.communicate()
        raise OSError(
            f"detached relay timed out after {_RELAY_TIMEOUT_SEC}s: {err.decode('utf-8', 'replace').strip()}"
        )

    pid_lines = out.decode("utf-8", "replace").strip().splitlines()
    if relay.returncode != 0 or not pid_lines:
        raise OSError(
            f"detached relay failed (returncode={relay.returncode}): {err.decode('utf-8', 'replace').strip()}"
        )
    try:
        pid = int(pid_lines[0])
    except ValueError:
        raise OSError(f"detached relay returned a non-numeric pid: {pid_lines[0]!r}")
    return _RelayedProcess(pid)


def popen_detached(args, *, cwd=None, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
    """親から切り離した子プロセスを起動する。"""
    if sys.platform == "win32":
        return _popen_detached_windows(args, cwd, stdout, stderr)
    return subprocess.Popen(args, cwd=cwd, stdout=stdout, stderr=stderr, start_new_session=True)
