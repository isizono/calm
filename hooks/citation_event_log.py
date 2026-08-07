"""hook共通: citation_event_log (migration 0046) への書き込みユーティリティ

sanitize_tool_result_hook.py / sanitize_backfill_hook.py の両方が使う
citation_event_log INSERT ロジックを集約する。src.services.citations_service の
record_citation_event と同じテーブルへ書くが、hooks は起動コストを抑えるため
src.db 経由の重い import (sqlite_vec / yoyo) を避け、sqlite3 を直接使う薄い実装に
している。source / verification_result の許容値だけは citations_service の
VALID_EVENT_SOURCES / VALID_VERIFICATION_RESULTS と手動で同期させておく
(migration 0046 の CHECK 制約と一致させる)。
"""
import json
import sqlite3
import sys

VALID_SOURCES = (
    "write_auto_convert",
    "bulk_migration",
    "transcript_post_tool_use",
    "transcript_session_start_backfill",
    "external_doc_sanitize",
)
VALID_VERIFICATION_RESULTS = ("exists", "dangling", "skip")


def log_event(
    db_path: str,
    *,
    source: str,
    hook_label: str,
    session_id: str | None,
    transcript_path: str | None,
    tool_name: str | None,
    before_text: str,
    after_text: str,
    verification_result: str | None,
    extra: dict,
) -> None:
    """citation_event_log に 1 行 INSERT する。

    write conn を別張りして close する。session_id/transcript_path はこのテーブルに
    専用カラムを持たないため extra_json に格納する。INSERT 自体の失敗
    (source/verification_result のバリデーションエラーを含む) は hook_label 付きで
    stderr に出すのみで例外を伝播しない (hook は非ブロックが原則のため)。
    """
    if source not in VALID_SOURCES:
        sys.stderr.write(
            f"[{hook_label}] citation_event_log write skipped: invalid source {source!r}\n"
        )
        return
    if (
        verification_result is not None
        and verification_result not in VALID_VERIFICATION_RESULTS
    ):
        sys.stderr.write(
            f"[{hook_label}] citation_event_log write skipped: "
            f"invalid verification_result {verification_result!r}\n"
        )
        return
    extra_json = json.dumps(
        {"session_id": session_id, "transcript_path": transcript_path, **extra},
        ensure_ascii=False,
    )
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                INSERT INTO citation_event_log (
                    source, tool_name, before_text, after_text,
                    verified_at, verification_result, extra_json
                ) VALUES (?, ?, ?, ?, datetime('now'), ?, ?)
                """,
                (source, tool_name, before_text, after_text, verification_result, extra_json),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        sys.stderr.write(f"[{hook_label}] citation_event_log write failed: {exc}\n")


def log_events_batch(
    db_path: str,
    *,
    source: str,
    hook_label: str,
    session_id: str | None,
    transcript_path: str | None,
    events: list[dict],
) -> None:
    """events (block/field 単位の変換結果) を citation_event_log へ一括 INSERT する。

    1 件ごとに connect/commit/close する log_event ではなく、1 connection・1
    トランザクションで executemany する (大規模 transcript での起動コストを抑える)。
    各 event は {"tool_name", "before_text", "after_text", "verification_result",
    "stats"} を持つ想定。verification_result が不正な event は個別にスキップし、
    残りは INSERT する。
    """
    if not events:
        return
    if source not in VALID_SOURCES:
        sys.stderr.write(
            f"[{hook_label}] citation_event_log batch write skipped: "
            f"invalid source {source!r}\n"
        )
        return
    rows = []
    for event in events:
        verification_result = event["verification_result"]
        if (
            verification_result is not None
            and verification_result not in VALID_VERIFICATION_RESULTS
        ):
            sys.stderr.write(
                f"[{hook_label}] citation_event_log batch write skipped event: "
                f"invalid verification_result {verification_result!r}\n"
            )
            continue
        extra_json = json.dumps(
            {
                "session_id": session_id,
                "transcript_path": transcript_path,
                "block_stats": event["stats"],
            },
            ensure_ascii=False,
        )
        rows.append(
            (
                source,
                event["tool_name"],
                event["before_text"],
                event["after_text"],
                verification_result,
                extra_json,
            )
        )
    if not rows:
        return
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executemany(
                """
                INSERT INTO citation_event_log (
                    source, tool_name, before_text, after_text,
                    verified_at, verification_result, extra_json
                ) VALUES (?, ?, ?, ?, datetime('now'), ?, ?)
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        sys.stderr.write(f"[{hook_label}] citation_event_log batch write failed: {exc}\n")
