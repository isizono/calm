"""retain 切れ時の publisher 直接 pull ヘルパ（relay-v2-sdk.md §3.5）。

retain 期間（default 24h）を超えた取りこぼしは relay outbox から消えている。subscriber は
publisher（cc-memory 等）へ直接 read を投げて補完する。SDK はこの経路を強制せず、
薄いラッパだけ提供する。labels namespace の解釈や差分検出は publisher 固有プロトコルの
責務であり、本ヘルパには含めない。
"""
from __future__ import annotations

from typing import Any, Callable, Iterator, Sequence


def reconcile(
    *,
    fetcher: Callable[[str | None], Iterator[Any]],
    labels: Sequence[str],
    since_ts: str | None = None,
) -> Iterator[Any]:
    """publisher 直接 pull の薄いラッパ（§3.5）。

    Args:
        fetcher: subscriber アプリ側が用意する関数。`since_ts` 文字列を受け取り、更新済み
            entity を順次 yield する callable（cc-memory なら get_map / search の light モード）。
        labels: 関心 labels。SDK は namespace 解釈を行わず、呼び出し側の記録用に受け取るだけ。
        since_ts: 直近 ack 済みの `delivered_at`。fetcher にそのまま渡す。

    Yields:
        fetcher の出力をそのまま。
    """
    yield from fetcher(since_ts)
