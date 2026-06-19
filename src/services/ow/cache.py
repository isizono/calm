"""ow runtime state ファイルキャッシュ (C-2案, A#911 SP-1, D#2654-2657).

真実源は relay events (`ow_history`)。本モジュールが書き出す JSON ファイルは
relay から再生成可能な派生キャッシュであり、破損・schema mismatch・channel
mismatch を検出した場合は即削除して None を返す (cache は再生成可能なので
backup は取らない、裁定 L3)。

配置:
    $OW_STATE_DIR (未設定時は ~/.cc-memory/ow/cache/)

ファイル名:
    topic-<id>.json  (1 topic = 1 file、最小粒度開始 / D#2655)

JSON 構造:
    schema_version をJSON先頭フィールドに置き (裁定 L4)、cat 観測で即座に
    schema 世代が分かる形にする。schema 変更時は CURRENT_SCHEMA_VERSION を
    bump → mismatch fallback で自動再構築する forward-only 戦略。
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 2


class OwState(TypedDict, total=False):
    """topic 単位の ow ランタイム状態キャッシュ。

    schema_version=2 で reducer fastpath 用フィールド ``identity_events`` / ``states`` /
    ``heartbeats`` を追加した (A#911 SP-2 PR-β)。1 → 2 の自動再構築は load_state の
    version mismatch fallback でハンドリングされる (forward-only)。

    EventEntry: ``{"msg_id": int, "data": dict, "created_at": str}``

    フィールド:
        schema_version: 現スキーマ世代 (CURRENT_SCHEMA_VERSION と等しい)
        channel: relay channel コード
        last_msg_id: projector が走査した最大 msg_id
        workers: handle → workload 軽量サマリ {task, state, latest_msg_id, latest_at}
        identities: handle → identity event の raw data dict (PR-α 形式、後方互換)
        identity_events: handle → identity EventEntry (PR-β 追加, reducer fastpath 用)
        states: handle → 最新 state EventEntry (PR-β 追加)
        heartbeats: handle → 最新 heartbeat EventEntry (PR-β 追加)
        presence: projection 時点の online handle (静的スナップショット)
        updated_at: projection を実行した UTC ISO8601
    """

    schema_version: int
    channel: str
    last_msg_id: int
    workers: dict[str, Any]
    identities: dict[str, Any]
    identity_events: dict[str, Any]
    states: dict[str, Any]
    heartbeats: dict[str, Any]
    presence: list[str]
    updated_at: str


def _get_state_dir() -> Path:
    """cache ディレクトリのパスを返す。

    OW_STATE_DIR 環境変数が設定されていればそのパスを使用する。
    未設定の場合は ~/.cc-memory/ow/cache/ をデフォルトとして返す (裁定 L5)。
    """
    env = os.environ.get("OW_STATE_DIR", "")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cc-memory" / "ow" / "cache"


def _state_file_path(topic_id: int) -> Path:
    return _get_state_dir() / f"topic-{topic_id}.json"


_TOPIC_FILE_RE = re.compile(r"^topic-(\d+)\.json$")


def find_topic_id_by_channel(channel: str) -> int | None:
    """cache ディレクトリを走査し、引数 channel と一致する OwState の topic_id を返す。

    reducer 4関数 (``ow_get_identity`` 等) は ``channel`` のみを受け取り
    ``topic_id`` を知らないため、cache fastpath で load_state を呼ぶには
    channel → topic_id の解決が必要。本ヘルパーは cache 物理ファイルの
    ``channel`` フィールドで線形検索する (A#911 SP-2 PR-β)。

    破損ファイル / version mismatch は **削除しない** (load_state 経路でない
    走査時の副作用を避ける)。schema_version も検証しない (channel フィールド
    の存在のみ確認)。

    Returns:
        最初に一致した topic_id、見つからなければ None。
    """
    state_dir = _get_state_dir()
    if not state_dir.exists():
        return None
    for path in state_dir.iterdir():
        m = _TOPIC_FILE_RE.match(path.name)
        if m is None:
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("channel") == channel:
            return int(m.group(1))
    return None


def load_state(topic_id: int, channel: str | None = None) -> OwState | None:
    """topic_id の state を JSON キャッシュから読む。

    以下4条件のいずれかに該当した場合は None を返す:
      1. キャッシュファイルが存在しない (削除はしない)
      2. JSON corruption (JSONDecodeError) — ファイル削除して None
      3. schema_version mismatch — ファイル削除して None
      4. channel 引数が指定され、cache 内 channel と不一致 — ファイル削除して None

    呼び出し側は None を受けたら relay full pull → save_state し直すこと。
    """
    path = _state_file_path(topic_id)
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        # exists() 後 open() 前に別プロセスが削除した競合 (TOCTOU)。
        # 不存在扱いで None を返し、ファイル削除は行わない。
        return None
    except (json.JSONDecodeError, OSError):
        logger.warning("ow state cache corruption at %s, deleting", path)
        path.unlink(missing_ok=True)
        return None

    cached_version = data.get("schema_version")
    if cached_version != CURRENT_SCHEMA_VERSION:
        logger.warning(
            "ow state cache schema_version mismatch at %s (got %r, expected %d), deleting",
            path,
            cached_version,
            CURRENT_SCHEMA_VERSION,
        )
        path.unlink(missing_ok=True)
        return None

    if channel is not None and data.get("channel") != channel:
        logger.warning(
            "ow state cache channel mismatch at %s (got %r, expected %r), deleting",
            path,
            data.get("channel"),
            channel,
        )
        path.unlink(missing_ok=True)
        return None

    return data  # type: ignore[return-value]


def save_state(topic_id: int, state: OwState) -> None:
    """state を JSON として書き出す。

    schema_version は呼び出し側の値より CURRENT_SCHEMA_VERSION を優先し、
    JSON 先頭フィールドに配置する (裁定 L4)。updated_at が未設定の場合は
    現在時刻 (UTC ISO8601) を埋める。

    書き込みは temp file + rename によるアトミック書き換え。
    """
    path = _state_file_path(topic_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    ordered: dict = {"schema_version": CURRENT_SCHEMA_VERSION}
    for key in (
        "channel",
        "last_msg_id",
        "workers",
        "identities",
        "identity_events",
        "states",
        "heartbeats",
        "presence",
        "updated_at",
    ):
        if key in state:
            ordered[key] = state[key]
    if "updated_at" not in ordered:
        ordered["updated_at"] = datetime.now(timezone.utc).isoformat()

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(ordered, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    finally:
        # json.dump TypeError や tmp.replace OSError で例外が出た場合に
        # .tmp ファイルが残らないようにする (成功時は replace 済みで既に存在しない)。
        tmp.unlink(missing_ok=True)
