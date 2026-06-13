#!/usr/bin/env python3 -u
"""SSEストリームから自分宛メッセージのみをstdoutに出力する。

使い方:
    curl -sN "${RELAY_URL}/stream?channel=${CHANNEL}&handle=${HANDLE}" \\
        | python3 -u recv_filter.py "${HANDLE}"

自分宛（to=自分のhandle）またはbroadcast（to="*"）のメッセージのみ出力する。
不正なJSON行はスキップ（クラッシュしない）。
python -u + 行ごとflushでMonitorへの遅延なし（D#2393）。
"""
import json
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: recv_filter.py <my_handle>", file=sys.stderr)
        sys.exit(1)

    my_handle = sys.argv[1]

    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line or not line.startswith("data: "):
            continue
        try:
            msg = json.loads(line[6:])
            body_raw = msg.get("body", "{}")
            body = json.loads(body_raw) if isinstance(body_raw, str) else body_raw
            to = body.get("to")
            if to == my_handle or to == "*":
                print(line, flush=True)
        except (json.JSONDecodeError, TypeError, KeyError):
            continue


if __name__ == "__main__":
    main()
