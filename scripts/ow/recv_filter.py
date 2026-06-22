#!/usr/bin/env python3 -u
"""SSEストリームから自分宛メッセージのみをstdoutに出力する。

使い方:
    curl -sN "${RELAY_URL}/stream?channel=${CHANNEL}&handle=${HANDLE}" \\
        | python3 -u recv_filter.py "${HANDLE}"

自分宛（to=自分のhandle）またはbroadcast（to="*"）のメッセージのみ出力する。
不正なJSON行はスキップ（クラッシュしない）。

envelope schema 検証:
    - v == 1 必須
    - kind in ("command", "event") 必須 (旧形式 envelope を drop)
    - data.type が存在すること
いずれかを欠く envelope は silent drop する。

task filter (opt-in):
    環境変数 OW_FILTER_TASK が設定されているとき、`task == OW_FILTER_TASK` を満たす
    envelope のみ通す。未設定なら無効 (後方互換)。他 task の broadcast event
    (identity / heartbeat 等) によるコンテキスト汚染を抑止する用途。

python -u + 行ごとflushでMonitorへの遅延なし。
"""
import json
import os
import sys


VALID_KINDS = frozenset({"command", "event"})


def _is_valid_envelope(body: dict) -> bool:
    """envelope schema (v / kind / data.type) を検証する。"""
    if body.get("v") != 1:
        return False
    if body.get("kind") not in VALID_KINDS:
        return False
    data = body.get("data")
    if not isinstance(data, dict):
        return False
    if not data.get("type"):
        return False
    return True


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: recv_filter.py <my_handle>", file=sys.stderr)
        sys.exit(1)

    my_handle = sys.argv[1]
    filter_task = os.environ.get("OW_FILTER_TASK") or None

    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line or not line.startswith("data: "):
            continue
        try:
            msg = json.loads(line[6:])
            body_raw = msg.get("body", "{}")
            body = json.loads(body_raw) if isinstance(body_raw, str) else body_raw
            if not isinstance(body, dict):
                continue

            if not _is_valid_envelope(body):
                continue

            to = body.get("to")
            if to != my_handle and to != "*":
                continue

            if filter_task is not None and body.get("task") != filter_task:
                continue

            print(line, flush=True)
        except (json.JSONDecodeError, TypeError, KeyError):
            continue


if __name__ == "__main__":
    main()
