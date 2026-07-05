"""dispatcher CLI entrypoint（`python -m relay_sdk.outbox`、relay-v2-sdk.md §2.3.2）。

引数は環境変数（§6）で渡す。`SIGTERM` / `SIGINT` 受領で終了処理に入り、in-flight な
`POST /publish` が返るのを最大 30 秒待ってから exit する。
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from src.relay_sdk import config as sdk_config
from src.relay_sdk.outbox.dispatcher import run_dispatcher

# in-flight リクエストの猶予（§2.3.2）。
_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 30.0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("RELAY_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("relay_sdk.outbox")

    db_path = os.environ.get("RELAY_OUTBOX_DB")
    if not db_path:
        logger.error("RELAY_OUTBOX_DB（dispatcher が見る SQLite ファイル）が必要です")
        return 2
    try:
        relay_base_url = sdk_config.env_base_url(None)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    stop_event = threading.Event()

    def _handle_signal(signum, _frame) -> None:
        logger.info("シグナル %s 受領、graceful shutdown 開始", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # run_dispatcher を別スレッドで回し、in-flight リクエストの猶予を join で担保する。
    thread = threading.Thread(
        target=run_dispatcher,
        kwargs=dict(
            db_path=db_path,
            relay_base_url=relay_base_url,
            agent_card_path=os.environ.get("RELAY_AGENT_CARD_PATH"),
            jws_key_path=os.environ.get("RELAY_JWS_KEY_PATH"),
            poll_interval_seconds=sdk_config.env_poll_interval_seconds(),
            max_retry=sdk_config.env_max_retry(),
            initial_backoff_seconds=sdk_config.env_initial_backoff_seconds(),
            backoff_factor=sdk_config.env_backoff_factor(),
            dlq_gc_interval_seconds=sdk_config.env_dlq_gc_interval_seconds(),
            http_timeout_seconds=sdk_config.env_http_timeout_seconds(),
            stop_event=stop_event,
        ),
        daemon=True,
    )
    thread.start()
    while thread.is_alive():
        thread.join(timeout=0.5)
    # stop_event が立った後、in-flight リクエストの猶予として最大 30 秒待つ
    # （run_dispatcher の finally が client.close() まで到達するのを待つ）。
    thread.join(timeout=_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
