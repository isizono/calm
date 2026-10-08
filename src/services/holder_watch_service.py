"""orchの担い手欄の見張り。

担い手のセッションが止まったら、signalと人宛てのaskを1回だけ立てる。担い手欄が
書き換わるとき（担い手の交代）には、旧担い手が作業中か、新しい状態節に仕込みの
証拠があるかを見て警告を返す。

どのClaudeセッションにも依存せずサーバーの中で動かすことで、担い手と常駐の
見張りが全部同時に落ちても止まったことが人に届くようにしている。生死とbusyは
Claude Code CLIが書く ``~/.claude/sessions/<pid>.json`` から、固まりはtranscriptの
最終更新時刻から読む。
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from pathlib import Path

from src.db import get_connection
from src.env_compat import env_get
from src.infra import cli_session
from src.infra.lock_file import is_process_alive
from src.services import ask_service, signal_service

logger = logging.getLogger(__name__)

INTERVAL_ENV = "CALM_HOLDER_WATCH_INTERVAL_SEC"
DEAD_MIN_ENV = "CALM_HOLDER_WATCH_DEAD_MIN"
STALE_MIN_ENV = "CALM_HOLDER_WATCH_STALE_MIN"
PROJECTS_DIR_ENV = "CALM_CLAUDE_PROJECTS_DIR"

DEFAULT_INTERVAL_SEC = 300
DEFAULT_DEAD_MIN = 10
# 担い手は30分ごとの起こし直しを仕込むので、idleでもtranscriptは30分おきに伸びる。
# その2倍を超えて伸びなければ、固まったか起こし直しが消えている。
DEFAULT_STALE_MIN = 60

# 裏のSAのtranscriptがこの秒数以内に更新されていれば、作業中とみなす
SUBAGENT_ACTIVE_SEC = 300

SIGNAL_KIND = "custom:holder-down"
SIGNAL_SOURCE = "watch:holder"

_HOLDER_LINE_RE = re.compile(r"^担い手[:：](.*)$", re.MULTILINE)
_SESSION_ID_RE = re.compile(r"sessionId\s+([0-9a-fA-F-]{36})")
_JOB_ID_RE = re.compile(r"job(?:\s*id)?\s*[:：]?\s*([0-9a-f]{8})", re.IGNORECASE)


def _read_float_env(name: str, default: float) -> float:
    raw = env_get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1
    if value < 0:
        print(f"[holder_watch] WARNING Invalid {name}={raw!r}, using {default}", file=sys.stderr)
        return default
    return value


def _holder_line(description: str | None) -> str | None:
    # 常駐の表などにもsessionIdが並ぶので、最初の担い手行だけを読む
    m = _HOLDER_LINE_RE.search(description or "")
    return m.group(1) if m else None


def holder_session_id(description: str | None) -> str | None:
    """説明の担い手欄のsessionId。担い手欄が無い・空席ならNone。"""
    line = _holder_line(description)
    if line is None:
        return None
    m = _SESSION_ID_RE.search(line)
    return m.group(1).lower() if m else None


def _holder_name(description: str | None) -> str:
    line = _holder_line(description) or ""
    return re.split(r"[（(]", line, maxsplit=1)[0].strip() or "（名前なし）"


def live_session(session_id: str) -> dict | None:
    """sessionIdが一致しpidが生きているCLIセッションの行。複数あればstartedAtが最新の行。"""
    best = None
    for path in cli_session.sessions_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("sessionId") != session_id:
            continue
        pid = data.get("pid")
        if not isinstance(pid, int) or not is_process_alive(pid):
            continue
        if best is None or (data.get("startedAt") or 0) > (best.get("startedAt") or 0):
            best = data
    return best


def _projects_dir() -> Path:
    raw = env_get(PROJECTS_DIR_ENV)
    return Path(raw).expanduser() if raw else Path.home() / ".claude" / "projects"


def _age_sec(pattern: str, now: float) -> float | None:
    mtimes = []
    for p in _projects_dir().glob(pattern):
        try:
            mtimes.append(p.stat().st_mtime)
        except OSError:
            continue
    return now - max(mtimes) if mtimes else None


def subagent_age_sec(session_id: str, now: float) -> float | None:
    """そのセッションが裏で走らせたSAのtranscriptの最終更新からの秒数。無ければNone。"""
    return _age_sec(f"*/{session_id}/subagents/*.jsonl", now)


def transcript_age_sec(session_id: str, now: float) -> float | None:
    """本体と裏のSAのtranscriptのうち最も新しい更新からの秒数。見つからなければNone。

    裏でSAが走っている間、窓口はidleのままで本体のtranscriptは伸びないので、
    SAの側も見ないと長いSAの最中を固まりと読んでしまう。
    """
    ages = [a for a in (_age_sec(f"*/{session_id}.jsonl", now),
                        subagent_age_sec(session_id, now)) if a is not None]
    return min(ages) if ages else None


def judge(live: dict | None, age_sec: float | None, dead_sec: float, stale_sec: float) -> str | None:
    """止まっていれば理由の文、止まっていなければNone。

    プロセスが無ければtranscriptはもう伸びないので、その最終更新からの経過を
    「死んでからの時間」とみなす（サーバーの再起動をまたいでも数え直さずに済む）。
    """
    if live is None:
        if age_sec is None or age_sec >= dead_sec:
            minutes = "不明" if age_sec is None else f"{age_sec / 60:.0f}分"
            return f"プロセスが無い（transcriptの最終更新から{minutes}）"
        return None
    if age_sec is not None and age_sec >= stale_sec:
        return f"transcriptが{age_sec / 60:.0f}分更新されていない（status {live.get('status')}）"
    return None


def _summary(activity_id: int, session_id: str) -> str:
    # 理由を含めない: 固まり→死亡と移っても、同じ担い手について1回だけにするため
    return f"orchの担い手が止まった: activity {activity_id} sessionId {session_id}"


def _already_fired(conn, summary: str) -> bool:
    fingerprint = signal_service._compute_fingerprint(SIGNAL_KIND, SIGNAL_SOURCE, summary)
    row = conn.execute(
        "SELECT 1 FROM signal_events WHERE fingerprint = ? LIMIT 1", (fingerprint,)
    ).fetchone()
    return row is not None


def _orch_activities(conn) -> list:
    return conn.execute(
        """
        SELECT a.id, a.title, a.description FROM activities a
        WHERE a.status != 'completed' AND EXISTS (
            SELECT 1 FROM activity_tags at JOIN tags t ON t.id = at.tag_id
            WHERE at.activity_id = a.id AND t.namespace = '' AND t.name = 'orch')
        """
    ).fetchall()


def _domain_tags(conn, activity_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.name FROM activity_tags at JOIN tags t ON t.id = at.tag_id
        WHERE at.activity_id = ? AND t.namespace = 'domain'
        """,
        (activity_id,),
    ).fetchall()
    return [f"domain:{r['name']}" for r in rows] or ["domain:calm"]


def check_once(dead_sec: float, stale_sec: float, now: float | None = None) -> list[dict]:
    """orchを1周見て、止まった担い手ごとにaskとsignalを立てる。立てたものを返す。"""
    now = time.time() if now is None else now
    fired = []
    conn = get_connection()
    try:
        orchs = _orch_activities(conn)
        for row in orchs:
            session_id = holder_session_id(row["description"])
            if session_id is None:
                continue
            summary = _summary(row["id"], session_id)
            if _already_fired(conn, summary):
                continue
            reason = judge(live_session(session_id), transcript_age_sec(session_id, now),
                           dead_sec, stale_sec)
            if reason is None:
                continue
            name = _holder_name(row["description"])
            # askを先に立てる。signalが先だと、ask作成に失敗したとき次の周で
            # 「立て済み」と読んで人に届かないまま終わる
            ask = ask_service.add_ask(
                question=(
                    f"orch「{row['title']}」の担い手 {name} が止まっている: {reason}。"
                    "担い手の窓口を確かめ、必要なら後継を起こしてほしい"
                ),
                blocks=[row["id"]],
                tags=_domain_tags(conn, row["id"]),
                context=(
                    f"CALMサーバーの担い手欄の見張りが立てた。sessionId {session_id}。"
                    "見張りは同じ担い手について1回しか知らせない。担い手欄が書き換われば"
                    "新しい担い手を見張り直す"
                ),
                notify=False,
            )
            if "error" in ask and "id" not in ask:
                logger.warning("holder watch: add_ask failed: %s", ask["error"])
                continue
            signal = signal_service.record_signal(
                SIGNAL_KIND, summary, source=SIGNAL_SOURCE, detail=reason,
                refs=[{"type": "activity", "id": row["id"]}],
                context={"session_id": session_id, "ask_id": ask.get("id")},
            )
            fired.append({"activity_id": row["id"], "session_id": session_id, "reason": reason,
                          "ask_id": ask.get("id"), "signal_id": signal["id"]})
    finally:
        conn.close()
    return fired


def handoff_warnings(old_description: str | None, new_description: str) -> list[str]:
    """担い手欄のsessionIdが書き換わるときの警告。書き込みは止めない。"""
    old_sid = holder_session_id(old_description)
    new_sid = holder_session_id(new_description)
    # 空席にする書き換えは本人が自分で行うことが多く、本人は必ずbusyなので見ない
    if new_sid is None or new_sid == old_sid:
        return []
    warnings = []
    if old_sid is not None:
        old = live_session(old_sid)
        if old is not None and old.get("status") == "busy":
            warnings.append(
                f"旧担い手（sessionId {old_sid}、pid {old.get('pid')}）がbusy。"
                "止める前に作業の終わりを待つか、旧担い手からの知らせを確かめる"
            )
        # 窓口がidleでも裏のSAが作業中のことがある。本体のtranscriptは見ない
        # （計画どおりの交代では旧担い手が直前に状態節を書いているので毎回新しい）
        sa_age = subagent_age_sec(old_sid, time.time()) if old is not None else None
        if sa_age is not None and sa_age < SUBAGENT_ACTIVE_SEC:
            warnings.append(
                f"旧担い手（sessionId {old_sid}）の裏のSAが{sa_age / 60:.0f}分前まで動いている。"
                "止めるとSAの作業が途中で切れる"
            )
    # 後継は旧状態節を写すので、旧担い手のjob idが残っていても仕込みの証拠にならない
    new_jobs = set(_JOB_ID_RE.findall(new_description)) - set(_JOB_ID_RE.findall(old_description or ""))
    if not new_jobs:
        warnings.append(
            "状態節に新しい起こし直し（CronCreate）のjob idが無い。"
            "仕込んでからjob idを状態節に書く"
        )
    return warnings


class HolderWatch:
    """check_onceを一定間隔で回すdaemon thread。interval<=0なら起動しない。"""

    def __init__(self, interval_sec: float | None = None, dead_sec: float | None = None,
                 stale_sec: float | None = None):
        self._interval = (interval_sec if interval_sec is not None
                          else _read_float_env(INTERVAL_ENV, DEFAULT_INTERVAL_SEC))
        self._dead_sec = (dead_sec if dead_sec is not None
                          else _read_float_env(DEAD_MIN_ENV, DEFAULT_DEAD_MIN) * 60)
        self._stale_sec = (stale_sec if stale_sec is not None
                           else _read_float_env(STALE_MIN_ENV, DEFAULT_STALE_MIN) * 60)
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._interval <= 0:
            logger.info("Holder watch disabled (interval<=0)")
            return
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop_event.set()

    def _loop(self) -> None:
        while not self._stop_event.wait(timeout=self._interval):
            try:
                for f in check_once(self._dead_sec, self._stale_sec):
                    logger.info("holder watch fired: %s", f)
            except Exception:
                logger.exception("holder watch check failed, continuing")
