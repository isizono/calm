#!/usr/bin/env python3
"""フィードバック機構の観測台帳の運用指標を表示する読み取り専用CLI（DBへは書き込まない）。

使い方:
    uv run python scripts/feedback_report.py

出力する5指標:
    (a) speakerの値の組（promptSource・turnOrigin・entrypoint・userType・
        isMeta・isSidechain）ごとの件数と、その組が最初に現れた行
    (b) 台帳の行数とバイト数（種類別、utteranceは人間の発話／人間でない発話の別）
    (c) 1セッションあたりの発話・応答・ツール呼び出し件数の分布
    (d) 人間の打鍵の許可リスト3組（typed/human・queued/human・sdk/human）の件数
    (e) hookが自分で取れた環境の値（cwd・agent_type・端末の手がかり等）の組
        ごとの件数。(a)とは別の表にする。判定には使われない値なので、組が
        増えても(a)の可読性に影響しない

(a)の「実測の26通りと比べられる形にする」は、この出力を実測レポートの表と
同じ列順で突き合わせられる形にすることで満たす（既知集合をこのスクリプトに
埋め込むと、実測データの解釈違いをコードへ固定化するリスクがあるため避けた）。
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.db import get_connection  # noqa: E402
from src.services.feedback_rules import ALLOWED_HUMAN_PROMPT_SOURCES  # noqa: E402

_SPEAKER_FIELDS = ("promptSource", "turnOrigin", "entrypoint", "userType", "isMeta", "isSidechain")

# speakerのtextに足された、transcriptの7キーの外にある観測用の生値（hooks/feedback_hook.py
# の_hook_context()のキーと一致させる）。判定には使われないので、(a)の7キーの組とは
# 別の表で数える。
_HOOK_CONTEXT_FIELDS = (
    "cwd",
    "transcript_path",
    "agent_type",
    "term",
    "term_program",
    "tmux",
    "sty",
    "ssh_tty",
    "stdin_isatty",
    "stdout_isatty",
    "claudecode",
    "claude_code_entrypoint",
    "claude_code_child_session",
)


def report_speaker_combos(conn) -> list[dict]:
    """(a) speakerの値の組ごとの件数と最初に現れた行。id昇順で最初の出現が分かる形。"""
    extract_cols = ", ".join(f"json_extract(text, '$.{f}') AS {f}" for f in _SPEAKER_FIELDS)
    group_cols = ", ".join(_SPEAKER_FIELDS)
    rows = conn.execute(
        f"""
        SELECT {extract_cols}, COUNT(*) AS cnt, MIN(id) AS first_id, MIN(created_at) AS first_seen_at
        FROM obs_events
        WHERE kind = 'speaker'
        GROUP BY {group_cols}
        ORDER BY first_id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def report_hook_context_combos(conn) -> list[dict]:
    """(e) 足した値（hookが自分で取れる環境の値）の組ごとの件数。(a)とは別の表。"""
    extract_cols = ", ".join(
        f"json_extract(text, '$.hook_context.{f}') AS {f}" for f in _HOOK_CONTEXT_FIELDS
    )
    group_cols = ", ".join(_HOOK_CONTEXT_FIELDS)
    rows = conn.execute(
        f"""
        SELECT {extract_cols}, COUNT(*) AS cnt, MIN(id) AS first_id
        FROM obs_events
        WHERE kind = 'speaker'
        GROUP BY {group_cols}
        ORDER BY first_id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def report_ledger_volume(conn) -> dict:
    """(b) 種類別の行数・バイト数。utteranceは人間の発話／人間でない発話の別も出す。"""
    by_kind = conn.execute(
        """
        SELECT kind, COUNT(*) AS rows_cnt,
               COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) AS bytes_sum
        FROM obs_events
        GROUP BY kind
        ORDER BY kind
        """
    ).fetchall()

    # utterance_human相当の判定をインラインで行う（ビューmigrationは別分割のため）
    human_row = conn.execute(
        f"""
        SELECT COUNT(*) AS rows_cnt, COALESCE(SUM(LENGTH(CAST(u.text AS BLOB))), 0) AS bytes_sum
        FROM obs_events u
        JOIN obs_events sp ON sp.kind = 'speaker' AND sp.ref_id = u.id
        WHERE u.kind = 'utterance' AND u.agent_id IS NULL
          AND json_extract(sp.text, '$.turnOrigin') = 'human'
          AND json_extract(sp.text, '$.promptSource') IN ({",".join("?" * len(ALLOWED_HUMAN_PROMPT_SOURCES))})
        """,
        tuple(ALLOWED_HUMAN_PROMPT_SOURCES),
    ).fetchone()
    total_utterance = conn.execute(
        "SELECT COUNT(*) AS rows_cnt, COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) AS bytes_sum "
        "FROM obs_events WHERE kind = 'utterance'"
    ).fetchone()

    return {
        "by_kind": [dict(row) for row in by_kind],
        "utterance_human": dict(human_row),
        "utterance_non_human": {
            "rows_cnt": total_utterance["rows_cnt"] - human_row["rows_cnt"],
            "bytes_sum": total_utterance["bytes_sum"] - human_row["bytes_sum"],
        },
    }


def _distribution(values: list[int]) -> dict:
    if not values:
        return {"n_sessions": 0, "min": 0, "max": 0, "mean": 0.0, "median": 0.0}
    return {
        "n_sessions": len(values),
        "min": min(values),
        "max": max(values),
        "mean": round(statistics.fmean(values), 2),
        "median": statistics.median(values),
    }


def report_per_session_distribution(conn) -> dict:
    """(c) 1セッションあたりの発話・応答・ツール呼び出し件数の分布。"""
    result = {}
    for label, kinds in (
        ("utterance", ("utterance",)),
        ("reply", ("reply",)),
        ("tool_call", ("tool", "tool_overflow")),
    ):
        placeholders = ",".join("?" * len(kinds))
        rows = conn.execute(
            f"SELECT session_id, COUNT(*) AS cnt FROM obs_events "
            f"WHERE kind IN ({placeholders}) GROUP BY session_id",
            kinds,
        ).fetchall()
        result[label] = _distribution([row["cnt"] for row in rows])
    return result


def report_allowlist_counts(conn) -> dict:
    """(d) 許可リスト3組（typed/queued/sdk × human）それぞれの件数。0件があれば値の変化を疑う。"""
    counts = {}
    for source in sorted(ALLOWED_HUMAN_PROMPT_SOURCES):
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM obs_events
            WHERE kind = 'speaker'
              AND json_extract(text, '$.promptSource') = ?
              AND json_extract(text, '$.turnOrigin') = 'human'
            """,
            (source,),
        ).fetchone()
        counts[f"{source}/human"] = row["cnt"]
    return counts


def _print_section(title: str) -> None:
    print(f"\n== {title} ==")


def main() -> int:
    conn = get_connection()
    try:
        _print_section("(a) speakerの値の組ごとの件数・初出")
        for row in report_speaker_combos(conn):
            print(row)

        _print_section("(b) 台帳の行数・バイト数")
        volume = report_ledger_volume(conn)
        for row in volume["by_kind"]:
            print(row)
        print("utterance(人間):", volume["utterance_human"])
        print("utterance(人間でない):", volume["utterance_non_human"])

        _print_section("(c) 1セッションあたりの件数分布")
        for label, dist in report_per_session_distribution(conn).items():
            print(label, dist)

        _print_section("(d) 許可リスト3組の件数")
        for combo, cnt in report_allowlist_counts(conn).items():
            flag = " <- 0件（値の変化を疑う）" if cnt == 0 else ""
            print(f"{combo}: {cnt}{flag}")

        _print_section("(e) 足した値（環境）の組ごとの件数")
        for row in report_hook_context_combos(conn):
            print(row)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
