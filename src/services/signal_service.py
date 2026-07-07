"""cc-memory 自身の故障報告・使用感不満・矛盾検出・運用計測イベントの記録サービス。

signal_events テーブルへの記録・取得・トリアージ状態遷移を担う。record_signal は
検証あり・例外を投げる通常経路。capture_signal_safe はいかなる例外も外に漏らさない
捕捉専用の薄いラッパで、middleware / hooks からの自動捕捉に使う。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import sys
from typing import Optional

from src.db import get_connection, row_to_dict
from src.services.readable_id import apply_readable_id_inplace

logger = logging.getLogger(__name__)

KNOWN_KINDS = {
    "machine_error",
    "friction",
    "contradiction",
    "precedent_miss",
    "precedent_misapplied",
    "boundary_case",
    "rollback",
}

VALID_STATUSES = {"new", "triaged", "promoted", "dismissed"}

# promoted_type として許可するエンティティ種別とその実体テーブル。
# pin_service.ENTITY_TABLE_MAP 等とは別に、signal_events の promoted_type CHECK
# (§migration コメント) が想定する 5 種のみに絞って定義する ('tag' 等は対象外)。
PROMOTED_ENTITY_TABLE = {
    "topic": "discussion_topics",
    "activity": "activities",
    "decision": "decisions",
    "log": "discussion_logs",
    "material": "materials",
}

# get_signals の limit 引数の上限。設計文書は上限を明記していないため、
# 他の一覧系サービス (get_decisions/get_logs の 30件上限) より広めの値を
# 実装判断で採用する (シグナルはトリアージ目的で多めに一覧したいケースがある)。
_MAX_LIMIT = 100

# stats.last_30d の集計期間。設計文書は「固定 vs 引数化」を実装者判断としており、
# v1 は固定で開始する。
_STATS_RECENT_DAYS = 30


def _normalize_summary(summary: str) -> str:
    """fingerprint計算用にsummaryを正規化する（前後空白除去 + 連続空白畳み込み + 小文字化）。"""
    return re.sub(r"\s+", " ", summary.strip()).lower()


def _compute_fingerprint(kind: str, source: str, summary: str) -> str:
    """sha256(kind|source|正規化summary) の先頭16hexをfingerprintとして返す。"""
    normalized = _normalize_summary(summary)
    digest = hashlib.sha256(f"{kind}|{source}|{normalized}".encode("utf-8")).hexdigest()
    return digest[:16]


def _to_json_or_raise(value: Optional[object], field_name: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field_name} is not JSON serializable: {e}") from e


def record_signal(
    kind: str,
    summary: str,
    *,
    source: str = "agent",
    detail: Optional[str] = None,
    refs: Optional[list[dict]] = None,
    context: Optional[dict] = None,
    session_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """検証あり・例外を投げる通常経路でシグナルを1件記録する。

    kind が KNOWN_KINDS に含まれない場合、summary が空の場合、refs/context が
    JSON serialize 不能な場合は ValueError を投げる。

    同一 fingerprint (sha256(kind|source|正規化summary) 先頭16hex) の
    status='new' 行が既存なら、新規行を作らず occurrence_count を +1 し
    last_seen_at / detail / refs / context / session_id を今回の値で上書きする
    （dedup、last-write-wins）。refs/context/session_id を最新 occurrence の値で
    更新するのは、contradiction のように refs で矛盾の両側を指す kind で
    2 回目以降の参照が失われるのを防ぐため、および再発したセッションを追える
    ようにするため。トリアージ済み（new 以外）の
    同型イベント再発は新規行になる。dedup 判定は idx_signal_fingerprint_new
    (部分 UNIQUE index) を conflict target にした INSERT ... ON CONFLICT で
    アトミックに行うため、並行書き込みでも競合が起きない。

    Args:
        kind: signal種別（machine_error/friction/contradiction/precedent_miss/
            precedent_misapplied/boundary_case/rollback のいずれか）
        summary: 1行要約（空文字不可）
        source: 発生源。'tool:<name>' / 'hook:<name>' / 'migration' / 'backup' /
            'agent' / 'user' / 'gate' 等
        detail: traceback・引数ダイジェスト・自由記述
        refs: [{"type": "decision", "id": 123}, ...] 形式の参照リスト
        context: kind ごとの構造化ペイロード
        session_id: 記録元セッションID
        conn: 呼び出し元が既に開いている接続を再利用する場合に渡す
            (二重接続を避けるための共有パターン)。省略時は自前で接続する

    Returns:
        {"id": int, "deduped": bool, "occurrence_count": int}
    """
    if kind not in KNOWN_KINDS:
        raise ValueError(f"Invalid kind: {kind!r}. Must be one of {sorted(KNOWN_KINDS)}")
    if not summary or not summary.strip():
        raise ValueError("summary must not be empty")

    refs_json = _to_json_or_raise(refs, "refs")
    context_json = _to_json_or_raise(context, "context")
    fingerprint = _compute_fingerprint(kind, source, summary)

    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO signal_events
                (kind, source, summary, detail, refs, context, fingerprint, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) WHERE status = 'new'
            DO UPDATE SET
                occurrence_count = signal_events.occurrence_count + 1,
                last_seen_at = CURRENT_TIMESTAMP,
                detail = excluded.detail,
                refs = excluded.refs,
                context = excluded.context,
                session_id = excluded.session_id
            RETURNING id, occurrence_count
            """,
            (kind, source, summary, detail, refs_json, context_json, fingerprint, session_id),
        )
        row = cursor.fetchone()
        signal_id, occurrence_count = row[0], row[1]
        if own_conn:
            conn.commit()
        # INSERT は常に occurrence_count=1 で始まるため、2 以上なら
        # ON CONFLICT 側 (dedup) が発火したことを意味する。
        return {
            "id": signal_id,
            "deduped": occurrence_count > 1,
            "occurrence_count": occurrence_count,
        }
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def capture_signal_safe(
    kind: str,
    summary: str,
    *,
    source: str = "agent",
    detail: Optional[str] = None,
    refs: Optional[list[dict]] = None,
    context: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> None:
    """捕捉経路用。いかなる例外も外に漏らさない (stderr へ出すのみ)。

    record_signal の validation エラー・DB 書き込み失敗を含め、あらゆる例外を
    握りつぶす。middleware / hooks の例外捕捉フックから安全に呼べる。
    """
    try:
        record_signal(
            kind,
            summary,
            source=source,
            detail=detail,
            refs=refs,
            context=context,
            session_id=session_id,
        )
    except Exception as e:
        print(f"capture_signal_safe failed: {type(e).__name__}: {e}", file=sys.stderr)


def get_signals(
    status: Optional[str] = "new",
    kind: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    include_stats: bool = False,
) -> dict:
    """シグナル一覧を取得する。

    Args:
        status: フィルタ対象のstatus。None指定で全status横断
        kind: フィルタ対象のkind。None指定で全kind横断
        limit: 取得件数上限（最大100件）
        offset: 取得開始位置
        include_stats: Trueのとき kind×status のクロス集計と直近30日サマリを付与

    Returns:
        {"signals": [...], "total_count": int, "stats": {...} (include_stats時のみ)}
        失敗時: {"error": {"code": ..., "message": ...}}
        各signalはidをid_rawへ退避しsession_id/fingerprintを含まない
        （_sanitize_signal_for_response参照）
    """
    if status is not None and status not in VALID_STATUSES:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)} or null",
            }
        }
    if kind is not None and kind not in KNOWN_KINDS:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"Invalid kind: {kind!r}. Must be one of {sorted(KNOWN_KINDS)} or null",
            }
        }

    limit = min(max(limit, 1), _MAX_LIMIT)
    offset = max(offset, 0)

    conn = get_connection()
    try:
        where_parts = []
        params: list[object] = []
        if status is not None:
            where_parts.append("status = ?")
            params.append(status)
        if kind is not None:
            where_parts.append("kind = ?")
            params.append(kind)
        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        total_count = conn.execute(
            f"SELECT COUNT(*) FROM signal_events {where_clause}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT * FROM signal_events {where_clause}
            ORDER BY last_seen_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

        signals = [_sanitize_signal_for_response(_signal_row_to_dict(row)) for row in rows]

        result: dict = {"signals": signals, "total_count": total_count}
        if include_stats:
            result["stats"] = _compute_stats(conn)
        return result
    except Exception as e:
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()


def _signal_row_to_dict(row: sqlite3.Row) -> dict:
    signal = row_to_dict(row)
    signal["refs"] = json.loads(signal["refs"]) if signal.get("refs") else None
    signal["context"] = json.loads(signal["context"]) if signal.get("context") else None
    return signal


def _sanitize_signal_for_response(signal: dict) -> dict:
    """get_signals / update_signal のレスポンス用にsignal dictを整形する(in-place)。

    session_id / fingerprintはrecord_signalのdedup・相関目的の内部専用フィールドで、
    他の read 系ツールがcaller_session_idを返却しない慣習と同様レスポンスから除去する。
    idは他のget系ツールと同じreadable_id変換でid_rawに退避する。

    signal自身のidだけでなく、他エンティティへの内部ID参照も同じ変換パターンで
    id_raw化する（timeline_serviceのreplaces/replaced_byと同様）:
    - refs配列内の各要素（{"type": ..., "id": 123}形式）
    - promoted_id（promoted_typeと対でエンティティ実体を指す。material_serviceの
      material_id同様id_key引数でid以外のキー名を指定する）
    - context内にネストした{"type": ..., "id": ...}形状の参照（precedent_missの
      missed_ids・precedent_misapplied のcited_id等、kindごとの規約キーを
      列挙しきれないため、キー名ではなく参照の形状で判定する）
    """
    signal.pop("session_id", None)
    signal.pop("fingerprint", None)

    refs = signal.get("refs")
    if isinstance(refs, list):
        for ref in refs:
            _apply_readable_id_if_ref_shaped(ref)

    promoted_type = signal.get("promoted_type")
    if promoted_type in PROMOTED_ENTITY_TABLE:
        apply_readable_id_inplace(signal, promoted_type, id_key="promoted_id")

    context = signal.get("context")
    if isinstance(context, (dict, list)):
        _apply_readable_id_recursive(context)

    apply_readable_id_inplace(signal, "signal")
    return signal


def _apply_readable_id_if_ref_shaped(value: object) -> None:
    """valueが{"type": <PROMOTED_ENTITY_TABLEのいずれか>, "id": int}形状ならid_raw化する。"""
    if isinstance(value, dict) and value.get("type") in PROMOTED_ENTITY_TABLE:
        apply_readable_id_inplace(value, value["type"])


def _apply_readable_id_recursive(value: object) -> None:
    """dict/listを再帰的に走査し、ネストしたエンティティ参照をすべてid_raw化する(in-place)。

    contextはkindごとに自由形式のペイロード（migration 0049コメント・
    report_signalの docstring 参照）で、missed_ids/cited_id等の規約キーを
    将来のkind追加も含めて列挙しきれない。そのためキー名ではなく
    refs/replaces/replaced_by と同じ{"type": ..., "id": ...}という参照の
    "形状"だけを手がかりに変換対象を判定する。
    """
    if isinstance(value, dict):
        _apply_readable_id_if_ref_shaped(value)
        for v in value.values():
            _apply_readable_id_recursive(v)
    elif isinstance(value, list):
        for item in value:
            _apply_readable_id_recursive(item)


def _compute_stats(conn: sqlite3.Connection) -> dict:
    """kind×status のクロス件数と直近 _STATS_RECENT_DAYS 日サマリを返す。

    フィルタ引数の影響を受けず、signal_events 全体を対象に集計する
    (トリアージ担当が全体像を把握するための集計であるため)。
    """
    by_kind_status: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT kind, status, COUNT(*) AS c FROM signal_events GROUP BY kind, status"
    ).fetchall():
        by_kind_status.setdefault(row["kind"], {})[row["status"]] = row["c"]

    last_period: dict[str, int] = {}
    for row in conn.execute(
        f"""
        SELECT kind, COUNT(*) AS c FROM signal_events
        WHERE first_seen_at >= datetime('now', '-{_STATS_RECENT_DAYS} days')
        GROUP BY kind
        """
    ).fetchall():
        last_period[row["kind"]] = row["c"]

    return {
        "by_kind_status": by_kind_status,
        f"last_{_STATS_RECENT_DAYS}d": last_period,
    }


def update_signal(
    signal_id: int,
    status: str,
    promoted_type: Optional[str] = None,
    promoted_id: Optional[int] = None,
) -> dict:
    """シグナルのトリアージ状態を遷移する。

    promoted_type/promoted_id は両方指定 or 両方省略のいずれかのみ許可する。
    両方指定時は昇格先エンティティの実在チェックを行った上でリンクする。
    省略時は既存の promoted_type/promoted_id を変更しない（status のみ更新）。

    last_seen_at は「最後に実際に再発した時刻」を表すため、トリアージ状態遷移では
    更新しない（更新は新規記録・dedup のみ）。人手のトリアージで last_seen_at を
    書き換えると get_signals のデフォルトソートが古いシグナルを上位へ押し上げる。

    Args:
        signal_id: 対象シグナルID
        status: 遷移先status（new/triaged/promoted/dismissed）
        promoted_type: 昇格先エンティティ種別（'topic'|'activity'|'decision'|'log'|'material'）
        promoted_id: 昇格先エンティティID

    Returns:
        {"signal": {...}}（更新後の行。get_signalsと同様idをid_rawへ退避し
        session_id/fingerprintを含まない）
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    if status not in VALID_STATUSES:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}",
            }
        }
    if (promoted_type is None) != (promoted_id is None):
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "promoted_type and promoted_id must be both set or both omitted",
            }
        }
    if promoted_type is not None and promoted_type not in PROMOTED_ENTITY_TABLE:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": (
                    f"Invalid promoted_type: {promoted_type!r}. "
                    f"Must be one of {sorted(PROMOTED_ENTITY_TABLE)}"
                ),
            }
        }

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM signal_events WHERE id = ?", (signal_id,)
        ).fetchone()
        if row is None:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"signal_events id={signal_id} not found",
                }
            }

        if promoted_type is not None:
            table = PROMOTED_ENTITY_TABLE[promoted_type]
            exists = conn.execute(
                f"SELECT 1 FROM {table} WHERE id = ?", (promoted_id,)
            ).fetchone()
            if not exists:
                return {
                    "error": {
                        "code": "NOT_FOUND",
                        "message": f"{promoted_type} id={promoted_id} does not exist",
                    }
                }
            conn.execute(
                """
                UPDATE signal_events
                SET status = ?, promoted_type = ?, promoted_id = ?
                WHERE id = ?
                """,
                (status, promoted_type, promoted_id, signal_id),
            )
        else:
            conn.execute(
                "UPDATE signal_events SET status = ? WHERE id = ?",
                (status, signal_id),
            )
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM signal_events WHERE id = ?", (signal_id,)
        ).fetchone()
        return {"signal": _sanitize_signal_for_response(_signal_row_to_dict(updated))}
    except Exception as e:
        conn.rollback()
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()
