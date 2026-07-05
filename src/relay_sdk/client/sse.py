"""SSE frame パーサ（relay-v2-sdk.md §4.2）。

`httpx` の streaming byte iterator から SSE frame（`event:` / `id:` / `data:` / comment）を
組み立てる。frame 境界は空行。comment 行（`:` 始まり、relay の `: keepalive`）も
`SSEFrame(kind="comment")` として yield し、呼び出し側（`Subscription.receive`）が
keepalive 契機の保守処理（lease renew / ack flush）を回せるようにする。

byte iterator を直接消費するのは、行/frame 単位で受信量を頭打ちにするため。行区切り
だけを httpx（`iter_lines`）に任せると、改行を送らないサーバに対して 1 行分のバッファが
無制限に伸びうる（httpx 側にサイズ上限が無い）。ここで自前に byte を境界分割し、
`max_buffer_bytes` / `max_frame_bytes` で頭打ちにする。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator


@dataclass(frozen=True)
class SSEFrame:
    # "event" | "comment" | "overflow"（overflow = 上限超過で破棄した frame の通知）。
    kind: str
    event: str | None = None
    id: str | None = None
    data: str | None = None
    comment: str | None = None


def parse_sse_byte_stream(
    chunks: Iterable[bytes],
    *,
    max_frame_bytes: int,
    max_buffer_bytes: int,
) -> Iterator[SSEFrame]:
    """SSE の byte chunk stream を frame に組み立てて yield する（frame 境界 = 空行）。

    frame 意味論は SSE 標準どおり:
    - 空行で 1 frame 確定。`data:` を持つ frame は `kind="event"`。
    - `:` 始まりの comment 行は即時に `kind="comment"` frame として yield する
      （keepalive 検出を frame 境界まで遅延させない）。
    - 未知フィールドは無視する。

    メモリ安全のため 2 つの上限を課す。どちらの超過でも壊れた frame を捨て、次の frame
    境界（空行）または stream 終端で同期を回復し、`kind="overflow"` frame を 1 度だけ
    yield して呼び出し側にログ/継続判断の契機を渡す:
    - ``max_buffer_bytes``: 改行が来ないまま 1 行分としてバッファできる byte 数の上限。
      サーバが改行を送らずにバイトを送り続けても、この上限で頭打ちにして読み飛ばす。
    - ``max_frame_bytes``: 1 frame の `data` として累積できる byte 数の上限。多数の
      `data:` 行 / 巨大 1 行で data_parts が無制限に伸びるのを防ぐ。
    """
    buf = bytearray()
    event_type: str | None = None
    event_id: str | None = None
    data_parts: list[str] = []
    frame_bytes = 0
    dropping = False  # 現 frame を空行まで捨てている（上限超過後の同期回復待ち）
    skipping_line = False  # 過大な 1 行を次の改行まで捨てている
    overflow_pending = False  # 破棄を overflow frame として 1 度だけ通知する

    def reset_frame() -> None:
        nonlocal event_type, event_id, data_parts, frame_bytes
        event_type = None
        event_id = None
        data_parts = []
        frame_bytes = 0

    for chunk in chunks:
        if not chunk:
            continue
        buf += chunk
        while True:
            nl = buf.find(b"\n")
            if nl == -1:
                # 未終端。改行が来ないまま上限超過なら、その行を過大とみなして
                # 以降の byte を次の改行まで読み飛ばし、現 frame を破棄する。
                if not skipping_line and len(buf) > max_buffer_bytes:
                    skipping_line = True
                    dropping = True
                    overflow_pending = True
                if skipping_line:
                    buf.clear()
                break
            raw = bytes(buf[:nl])
            del buf[: nl + 1]
            if skipping_line:
                # 過大行の末尾（改行）に到達。行は捨て、frame は空行まで破棄継続。
                skipping_line = False
                continue
            line = raw.rstrip(b"\r").decode("utf-8", "replace")

            if line == "":
                # frame 境界。
                if dropping:
                    dropping = False
                    reset_frame()
                    if overflow_pending:
                        overflow_pending = False
                        yield SSEFrame(kind="overflow")
                    continue
                if data_parts:
                    yield SSEFrame(
                        kind="event",
                        event=event_type,
                        id=event_id,
                        data="\n".join(data_parts),
                    )
                reset_frame()
                continue

            if dropping:
                continue

            if line.startswith(":"):
                yield SSEFrame(kind="comment", comment=line[1:].lstrip())
                continue

            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event_type = value
            elif field == "id":
                event_id = value
            elif field == "data":
                if frame_bytes + len(raw) > max_frame_bytes:
                    # frame 過大 → 空行まで破棄して同期回復する。
                    dropping = True
                    overflow_pending = True
                    reset_frame()
                    continue
                frame_bytes += len(raw)
                data_parts.append(value)
            # 未知フィールドは無視（SSE 仕様）。

    # stream 終端で未確定 frame が残っていれば flush（破棄中の frame は捨てたまま）。
    if not dropping and data_parts:
        yield SSEFrame(
            kind="event",
            event=event_type,
            id=event_id,
            data="\n".join(data_parts),
        )
    if overflow_pending:
        yield SSEFrame(kind="overflow")
