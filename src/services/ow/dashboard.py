"""ow_dashboard: relay履歴 → ow_workers MV → activities を経て生成する読み取り専用
ビュー。SessionStart hook / orch tool / check_in summary / `.views/` ファイル / 人間 cat /
CI log のすべてで同じ文字列を共有する単一レンダラ（M#288 R11）。

スタイル指針 (M#288 §6.0):
- UTF-8 LF / 行幅100文字以内 / カラーコード未出力 / 絵文字は ●◐○⚠✅ のみ / box-drawing禁止
- activity 行: `<icon> [<activity_id>] [<intent>] <title> ─ <status>[ (<worker_presence>)]`
- check_in からの行抽出ヘルパー `extract_activity_line` が正規表現で行を切り出すため、
  activity_id は必ず `[<数字>]` 形式で出す
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.db import get_connection
from src.services.ow import workers as wk
from src.services.ow.projector import (
    OW_MANAGED_TAG,
    ow_project_activities_with_conn,
)
from src.services.ow.reducer import ow_apply_state_with_conn
from src.services.tag_service import (
    get_entity_tags,
    get_entity_tags_batch,
)


_VIEWS_DIR_ENV = "OW_VIEWS_DIR"
_DEFAULT_VIEWS_SUBPATH = Path(".cc-memory") / "ow" / ".views"

# 一般ビューでの行幅上限 (M#288 §6.0)
_MAX_LINE_WIDTH = 100

_STATUS_ICON = {
    "in_progress": "●",
    "pending": "◐",
    "completed": "○",
    "snoozed": "◐",
    "shelved": "◐",
}
_STALLED_ICON = "⚠"

_INTENT_LABEL = {
    "design": "[設計]",
    "discuss": "[議論]",
    "implement": "[作業]",
    "investigate": "[調査]",
    "review": "[レビュー]",
}


def _views_dir() -> Path:
    """`.views/` 配下のパス。env で上書き可能。"""
    override = os.environ.get(_VIEWS_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / _DEFAULT_VIEWS_SUBPATH


def view_file_path(topic_id: int) -> Path:
    return _views_dir() / f"dashboard-t{topic_id}.md"


def write_view_file_atomic(topic_id: int, content: str) -> Path:
    """temp + atomic rename で `.views/dashboard-t<topic>.md` を書き出す。"""
    target = view_file_path(topic_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f"dashboard-t{topic_id}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return target


def extract_activity_line(text: str, activity_id: int) -> str | None:
    """render結果から activity_id を含む行を抽出する。

    行頭は ●◐○⚠✅ のいずれかで、その後に `[<activity_id>]` が来る形式。
    複数行マッチした場合は先頭を返す。マッチなしは None。
    """
    pattern = re.compile(
        rf"^[●◐○⚠✅]\s+\[{activity_id}\]\s+.*$", re.MULTILINE
    )
    m = pattern.search(text)
    return m.group(0) if m else None


def _intent_label_for(tags: list[str]) -> str:
    """activity tags から intent ラベル文字列を取り出す。なければ空文字。"""
    for t in tags:
        if t.startswith("intent:"):
            intent = t.split(":", 1)[1]
            return _INTENT_LABEL.get(intent, f"[{intent}]")
    return ""


def _format_age_seconds(now_iso: str, then_iso: str | None) -> str:
    if not then_iso:
        return ""
    try:
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        then = datetime.fromisoformat(then_iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = int((now - then).total_seconds())
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    return f"{delta // 3600}h"


def _visual_width(text: str) -> int:
    """全角を 2、半角を 1 として簡易幅計算する。"""
    return sum(2 if ord(c) > 0x7F else 1 for c in text)


def _truncate(text: str, max_width: int) -> str:
    """全角を 2、半角を 1 として簡易計算で省略する。

    末尾の `…` (幅2) を含めて max_width に収まるよう調整する。
    """
    if _visual_width(text) <= max_width:
        return text
    # `…` (幅2) を確保するため max_width - 2 まで詰める
    budget = max(max_width - 2, 0)
    width = 0
    out = []
    for ch in text:
        w = 2 if ord(ch) > 0x7F else 1
        if width + w > budget:
            break
        out.append(ch)
        width += w
    return "".join(out) + "…"


def _format_activity_row(
    activity: dict,
    intent_label: str,
    workers: list[dict],
    now_iso: str,
) -> str:
    """1 activity の行を組み立てる。"""
    status = activity["status"]
    icon = _STATUS_ICON.get(status, _STALLED_ICON)
    title = activity["title"] or ""
    presence_parts: list[str] = []
    for w in workers:
        if w["workload_state"] == "terminated":
            continue
        age = _format_age_seconds(now_iso, w.get("last_heartbeat_at"))
        if age:
            presence_parts.append(
                f"{w['alias']} {w['workload_state']}, hb {age} ago"
            )
        else:
            presence_parts.append(f"{w['alias']} {w['workload_state']}")
    presence = ""
    if presence_parts:
        presence = " (" + ", ".join(presence_parts) + ")"
    intent_part = f"{intent_label} " if intent_label else ""
    fixed_part = f"{icon} [{activity['id']}] {intent_part}"
    suffix = f" ─ {status}{presence}"
    title_budget = max(
        _MAX_LINE_WIDTH - _visual_width(fixed_part) - _visual_width(suffix), 10,
    )
    truncated_title = _truncate(title, title_budget)
    return f"{fixed_part}{truncated_title}{suffix}"


def _format_orch_worker_detail(w: dict, now_iso: str) -> str:
    age = _format_age_seconds(now_iso, w.get("last_heartbeat_at")) or "?"
    state = w["workload_state"]
    return (
        f"- {w['alias']:<8} T{w['task_n']:<3} activity#{w.get('activity_id') or '?':<5} "
        f"workload={state:<10} hb={age:<5} "
        f"model={w.get('model') or '?'}  cwd={w.get('cwd') or '?'}"
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _managed_activities_for_topic(
    conn: sqlite3.Connection, topic_id: int
) -> list[dict]:
    """ow:managed 付きの activity を topic に紐づくぶんだけ返す。"""
    rows = conn.execute(
        """
        SELECT DISTINCT a.id, a.title, a.status, a.last_heartbeat_at
        FROM activities a
        JOIN activity_tags at ON at.activity_id = a.id
        JOIN tags t ON t.id = at.tag_id
        JOIN relations r
          ON r.source_type = 'activity' AND r.source_id = a.id
         AND r.target_type = 'topic' AND r.target_id = ?
        WHERE t.namespace = 'ow' AND t.name = 'managed'
        ORDER BY a.id
        """,
        (topic_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _alive_workers_for_topic(
    conn: sqlite3.Connection, topic_id: int
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM ow_workers
        WHERE topic_id = ? AND workload_state != 'terminated'
        ORDER BY task_n
        """,
        (topic_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _recently_terminated_workers_for_topic(
    conn: sqlite3.Connection, topic_id: int, limit: int = 10
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM ow_workers
        WHERE topic_id = ? AND workload_state = 'terminated'
        ORDER BY terminated_at DESC, id DESC
        LIMIT ?
        """,
        (topic_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def render_with_conn(
    conn: sqlite3.Connection,
    *,
    topic_id: int,
    role: str = "general",
    now_iso: str | None = None,
) -> str:
    """ow:managed activity 一覧 + worker presence をレンダリングする。

    role="general" は activity 一覧 + alive worker サマリ。
    role="orch" は同一内容 + alive/terminated worker の詳細。
    """
    now = now_iso or _utc_now_iso()
    activities = _managed_activities_for_topic(conn, topic_id)
    workers_by_activity: dict[int, list[dict]] = {}
    for w in _alive_workers_for_topic(conn, topic_id):
        if w.get("activity_id"):
            workers_by_activity.setdefault(w["activity_id"], []).append(w)
    tags_batch = get_entity_tags_batch(
        conn, "activity_tags", "activity_id", [a["id"] for a in activities]
    ) if activities else {}

    lines: list[str] = [f"## アクティビティ一覧（topic t{topic_id}）", ""]
    if not activities:
        lines.append("（ow:managed の activity がありません）")
    for a in activities:
        intent_label = _intent_label_for(tags_batch.get(a["id"], []))
        workers = workers_by_activity.get(a["id"], [])
        lines.append(_format_activity_row(a, intent_label, workers, now))

    if role == "orch":
        lines.append("")
        lines.append(f"## orch詳細ビュー topic t{topic_id}")
        lines.append("")
        alive_all = _alive_workers_for_topic(conn, topic_id)
        lines.append("### Alive workers (workload_state != terminated)")
        if alive_all:
            for w in alive_all:
                lines.append(_format_orch_worker_detail(w, now))
        else:
            lines.append("(empty)")
        lines.append("")
        lines.append("### Recently terminated（直近10件）")
        term = _recently_terminated_workers_for_topic(conn, topic_id)
        if term:
            for w in term:
                cause = w.get("cause") or "?"
                tat = w.get("terminated_at") or ""
                lines.append(
                    f"- {w['alias']:<8} T{w['task_n']:<3} "
                    f"activity#{w.get('activity_id') or '?'}  "
                    f"terminated({cause})  at {tat}"
                )
        else:
            lines.append("(empty)")

    return "\n".join(lines) + "\n"


def render_dashboard(
    *,
    topic_id: int,
    role: str = "general",
    history: list[dict] | None = None,
    channel_code: str | None = None,
    write_view: bool = True,
    apply_state: bool = True,
    now_iso: str | None = None,
) -> dict:
    """ダッシュボードレンダリングのエントリポイント。

    動作:
      1. apply_state=True かつ history が渡された場合は reducer を lazy 実行
      2. projector を ow:managed activity に対して実行
      3. role に応じてレンダー
      4. write_view=True なら `.views/dashboard-t<topic>.md` を atomic write

    Returns:
        {
            "topic_id": int,
            "rendered": str,
            "view_file": str | None,
            "reduced": {...} | None,
            "projected": [...] | None,
        }
    """
    now = now_iso or _utc_now_iso()
    conn = get_connection()
    reduced = None
    projected = None
    try:
        if apply_state and history and channel_code:
            reduced = ow_apply_state_with_conn(
                conn,
                channel_code=channel_code,
                topic_id=topic_id,
                history=history,
                now=now,
            )
        projected = ow_project_activities_with_conn(conn, topic_id=topic_id)
        conn.commit()
        rendered = render_with_conn(
            conn, topic_id=topic_id, role=role, now_iso=now,
        )
    finally:
        conn.close()
    view_path = None
    if write_view:
        view_path = str(write_view_file_atomic(topic_id, rendered))
    return {
        "topic_id": topic_id,
        "rendered": rendered,
        "view_file": view_path,
        "reduced": reduced,
        "projected": projected,
    }


def is_ow_managed_activity_with_conn(
    conn: sqlite3.Connection, activity_id: int
) -> bool:
    """activity が ow:managed タグを持つか。check_in 統合判定用。"""
    tags = get_entity_tags(conn, "activity_tags", "activity_id", activity_id)
    return OW_MANAGED_TAG in tags
