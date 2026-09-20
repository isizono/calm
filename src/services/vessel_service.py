"""自己改善ループの器: 知見を作る・追記する・引く3つの書き込み/読み取り処理。

このモジュールは observe/on を問わず get_lessons が動く前提で書く。record_lesson・
append_lesson だけが vessel_meta.mode='off' を見て拒否する（observeでは書ける。
配達しないので振る舞いには影響しない）。

**引用（quote）の自動変換をこのテーブル群には適用しない**: quoteは人間の発話からの
逐語であり、書き換えると引用の照合（部分文字列一致によるvessel:knowledge出自判定）
が成り立たなくなるため、citations_service の変換パイプラインを通さない。
"""
from __future__ import annotations

import json
import re
import sqlite3

from src.db import get_connection
from src.services.search_service import _escape_fts5_query
from src.services.vessel_rules import _trigrams, canonical_spec, similarity

FTS_CANDIDATE_TRIGRAM_CAP = 24

SESSION_POOL_MAX_CHARS = 1500
SIMILAR_REJECT_THRESHOLD = 0.55
SIMILAR_WARN_THRESHOLD = 0.40
SIMILAR_CANDIDATE_LIMIT = 3

# トリガーが投げる 'vessel:<理由>' を拒否コードへ写す表。表に無い理由・その他の
# 例外（NOT NULL/CHECK違反等）はすべて db_rejected に落ちる。
_TRIGGER_REJECT_CODES = {
    "vessel:kind_mismatch": "kind_mismatch",
    "vessel:no_session_channel": "no_session_channel",
}

_FIX_HINTS = {
    "vessel_off": "vessel_meta.mode が 'on' か 'observe' になるまで待つ",
    "invalid_spec": "条件JSONの field/op/value と正規表現の構文を見直す",
    "kind_mismatch": "kindと条件(deliver_event/step_event)の組み合わせをlesson_kindsに合わせる",
    "seed_only": "踏み跡条件も書けるならprevent、条件を書けない判断の癖ならtallyにする",
    "no_session_channel": "deliver_eventをsession以外にする",
    "duplicate": "append_lessonで追記するか、条件を変える",
    "similar_exists": "同じ知見ならappend_lesson(kind='violated')、別物ならnot_same_asに並べて再実行",
    "session_pool_full": "常時配達(session)以外の口にするか、既存の常時知見を撤回する",
    "protected": "本文・条件・撤回は守られた知見には効かない。noteかappend_lesson(kind='violated'/'contradicted')を使う",
    "unknown_lesson": "handleを確認する（撤回済みのhandleは再利用できない）",
    "db_rejected": "エラーメッセージを確認して引数を見直す",
}


def _reject(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "fix": _FIX_HINTS.get(code, "引数を見直す")}}


def _db_error_to_reject(exc: sqlite3.IntegrityError) -> dict:
    msg = str(exc)
    for prefix, code in _TRIGGER_REJECT_CODES.items():
        if prefix in msg:
            return _reject(code, msg)
    return _reject("db_rejected", msg)


class _Rejected(Exception):
    """トランザクション内の拒否をrollbackまで一気に伝えるための内部シグナル。"""

    def __init__(self, result: dict):
        self.result = result


def _check_mode_not_off(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT mode FROM vessel_meta WHERE id = 1").fetchone()
    if row is None or row["mode"] == "off":
        raise _Rejected(_reject("vessel_off", "vessel_meta.mode が off"))


def _validate_spec_format(event: str, spec: dict) -> None:
    """条件JSONの形を検査する。壊れていれば ValueError/KeyError/TypeError/re.error を投げる。

    canonical_spec() より前に必ず呼ぶこと（canonical_spec()は形の壊れたspecに
    対してKeyError/TypeErrorを投げるため、ここで先に弾かないとその例外が
    そのまま外へ漏れる）。
    """
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    if event == "session":
        if spec:
            raise ValueError("session の配達条件は {} 固定")
        return
    tool = spec.get("tool")
    if tool is not None:
        if event not in ("tool_call", "tool_fail"):
            raise ValueError("tool は tool_call/tool_fail のときだけ書ける")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("tool は非空文字列")
    clauses = spec.get("all", [])
    if not isinstance(clauses, list) or len(clauses) > 3:
        raise ValueError("all は最大3要素の配列")
    for c in clauses:
        if not isinstance(c, dict) or set(c) != {"field", "op", "value"}:
            raise ValueError("all の各要素は field/op/value の3キーのみ")
        field, op, value = c["field"], c["op"], c["value"]
        if not isinstance(field, str) or not field.strip():
            raise ValueError("field は非空文字列")
        if op not in ("regex", "len_gt"):
            raise ValueError("op は regex/len_gt のいずれか")
        if op == "regex":
            if not isinstance(value, str) or len(value) > 200:
                raise ValueError("regexのvalueは200字までの文字列")
            re.compile(value)
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("len_gtのvalueは数値")


def _lesson_kind_shape(conn: sqlite3.Connection, kind: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT delivers, steps FROM lesson_kinds WHERE kind = ?", (kind,)
    ).fetchone()
    if row is None:
        raise _Rejected(_reject("kind_mismatch", f"未知のkind: {kind}"))
    return row


def _normalize_spec_pair(event: str | None, spec) -> tuple[str | None, str | None]:
    """(event, spec引数) を検査し、(event, canonical_specの文字列) を返す。

    どちらもNoneならそのまま(None, None)。片方だけ与えられた場合もinvalid_specにする
    （lessons/lesson_entriesのCHECK (deliver_event IS NULL) = (deliver_spec IS NULL) と
    同じ形を先に検査することで、その違反がdb_rejectedとして曖昧に出るのを防ぐ）。
    """
    if event is None and spec is None:
        return None, None
    if event is None or spec is None:
        raise ValueError("event と spec は両方指定するか両方省略する")
    if isinstance(spec, str):
        spec = json.loads(spec)
    _validate_spec_format(event, spec)
    return event, canonical_spec(spec)


def _kind_shape_matches(shape: sqlite3.Row, deliver_event, step_event) -> bool:
    return (deliver_event is not None) == bool(shape["delivers"]) and (
        step_event is not None
    ) == bool(shape["steps"])


def _session_pool_total_excluding(conn: sqlite3.Connection, exclude_lesson_id: int | None) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(LENGTH(lc.body)), 0) AS total
        FROM lesson_current lc
        JOIN lesson_origin lo ON lo.lesson_id = lc.lesson_id
        WHERE lc.retracted = 0 AND lc.deliver_event = 'session' AND lo.origin = 'human'
          AND lc.lesson_id != ?
        """,
        (exclude_lesson_id if exclude_lesson_id is not None else -1,),
    ).fetchone()
    return row["total"]


def _check_session_pool(conn: sqlite3.Connection, exclude_lesson_id: int | None, added_body: str) -> None:
    total = _session_pool_total_excluding(conn, exclude_lesson_id) + len(added_body)
    if total > SESSION_POOL_MAX_CHARS:
        raise _Rejected(_reject(
            "session_pool_full",
            f"常時枠({SESSION_POOL_MAX_CHARS}字)を超える: 合計{total}字",
        ))


def _unknown_tool_warning(conn: sqlite3.Connection, spec_json: dict | None) -> str | None:
    tool = (spec_json or {}).get("tool")
    if not tool:
        return None
    row = conn.execute(
        "SELECT 1 FROM obs_events WHERE tool_name = ? LIMIT 1", (tool,)
    ).fetchone()
    if row is None:
        return f"tool名 '{tool}' は台帳に一度も現れていない（短い名前で書いた場合は要確認）"
    return None


def _similarity_check(conn: sqlite3.Connection, new_body: str, new_quote, not_same_as) -> dict | list[str]:
    """類似の検査。拒否辞書、またはwarnings文字列のリストを返す。

    候補の絞り込み（lessons_fts, tokenize='trigram'）は本文の文字trigramを
    そのままOR照合に使う（本文は空白の無い日本語文がほとんどで、単語分割では
    lessons_fts自身のトークン単位と噛み合わないため）。一致の度合いの判定は
    ここではなく、この後Python側でDice係数により行う。
    """
    not_same_as = set(not_same_as or [])
    grams = list(_trigrams(new_body))[:FTS_CANDIDATE_TRIGRAM_CAP]
    if not grams:
        return []
    query = " OR ".join(_escape_fts5_query(g) for g in grams)
    try:
        rows = conn.execute(
            """
            SELECT l.handle, l.body, l.quote
            FROM lessons_fts
            JOIN lessons l ON l.id = lessons_fts.rowid
            JOIN lesson_current lc ON lc.lesson_id = l.id
            WHERE lessons_fts MATCH ? AND lc.retracted = 0
            ORDER BY bm25(lessons_fts)
            LIMIT ?
            """,
            (query, SIMILAR_CANDIDATE_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        # MATCH構文自体が不正になるクエリ（記号だけの語など）は候補無しとして扱う。
        return []
    warnings = []
    for r in rows:
        score = max(similarity(new_body, r["body"]), similarity(new_quote or "", r["quote"] or ""))
        if score >= SIMILAR_REJECT_THRESHOLD and r["handle"] not in not_same_as:
            return _reject(
                "similar_exists",
                f"類似の既存知見がある: {r['handle']}（一致度{score:.2f}）",
            )
        if SIMILAR_WARN_THRESHOLD <= score < SIMILAR_REJECT_THRESHOLD:
            warnings.append(f"近い知見がある: {r['handle']}（一致度{score:.2f}）")
    return warnings


def record_lesson(
    kind: str,
    handle: str,
    body: str,
    deliver_event: str | None = None,
    deliver_spec=None,
    step_event: str | None = None,
    step_spec=None,
    quote: str | None = None,
    not_same_as: list[str] | None = None,
) -> dict:
    """知見を1件作る。

    検査は1つのトランザクション(BEGIN IMMEDIATE)の中で行い、並列呼び出しが
    同じ条件で2件作るのを防ぐ。拒否はすべて {"ok": false, "error": {...}} で
    返り、例外は投げない。
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_mode_not_off(conn)

        shape = _lesson_kind_shape(conn, kind)
        if shape["delivers"] and not shape["steps"]:
            raise _Rejected(_reject("seed_only", f"{kind} は初期値専用（踏み跡条件を持たない配達する種類）"))

        try:
            deliver_event, deliver_spec_norm = _normalize_spec_pair(deliver_event, deliver_spec)
            step_event, step_spec_norm = _normalize_spec_pair(step_event, step_spec)
        except (ValueError, KeyError, TypeError, re.error) as e:
            raise _Rejected(_reject("invalid_spec", str(e))) from e

        if not _kind_shape_matches(shape, deliver_event, step_event):
            raise _Rejected(_reject("kind_mismatch", f"{kind} の条件の有無が合わない"))

        # handleの重複は撤回済みも含めて弾く（handleは再利用できない。lessons.handleの
        # UNIQUE制約と同じ範囲をここで先に見て、通常の拒否として返す）。
        if conn.execute("SELECT 1 FROM lessons WHERE handle = ?", (handle,)).fetchone() is not None:
            raise _Rejected(_reject("duplicate", f"handleが既存の知見と重複: {handle}"))

        dup = conn.execute(
            """
            SELECT handle FROM lesson_current
            WHERE retracted = 0
              AND COALESCE(deliver_event,'') = ? AND COALESCE(deliver_spec,'') = ?
              AND COALESCE(step_event,'')    = ? AND COALESCE(step_spec,'')    = ?
            """,
            (deliver_event or "", deliver_spec_norm or "", step_event or "", step_spec_norm or ""),
        ).fetchone()
        if dup is not None:
            raise _Rejected(_reject("duplicate", f"既存の知見と条件が完全一致: {dup['handle']}"))

        sim = _similarity_check(conn, body, quote, not_same_as)
        if isinstance(sim, dict):
            raise _Rejected(sim)
        warnings = list(sim)

        if deliver_event == "session":
            _check_session_pool(conn, None, body)

        try:
            deliver_spec_obj = json.loads(deliver_spec) if isinstance(deliver_spec, str) else deliver_spec
        except (TypeError, ValueError):
            deliver_spec_obj = None
        tool_warning = _unknown_tool_warning(conn, deliver_spec_obj)
        if tool_warning:
            warnings.append(tool_warning)

        try:
            cur = conn.execute(
                """
                INSERT INTO lessons (kind, handle, body, deliver_event, deliver_spec,
                                      step_event, step_spec, quote)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (kind, handle, body, deliver_event, deliver_spec_norm, step_event, step_spec_norm, quote),
            )
        except sqlite3.IntegrityError as e:
            raise _Rejected(_db_error_to_reject(e)) from e

        lesson_id = cur.lastrowid
        conn.commit()
        return {"ok": True, "handle": handle, "lesson_id": lesson_id, "warnings": warnings}
    except _Rejected as r:
        conn.rollback()
        return r.result
    except Exception as e:  # noqa: BLE001 - 拒否を例外にしない、という契約上の不変条件
        conn.rollback()
        return _reject("db_rejected", str(e))
    finally:
        conn.close()


_ENTRY_KINDS = {"body", "conditions", "note", "violated", "contradicted", "withdraw"}
_PROTECTED_BLOCKS = {"body", "conditions", "withdraw"}


def append_lesson(
    handle: str,
    kind: str,
    body: str | None = None,
    note: str | None = None,
    deliver_event: str | None = None,
    deliver_spec=None,
    step_event: str | None = None,
    step_spec=None,
    quote: str | None = None,
) -> dict:
    """知見に1件追記する。

    守られた知見への body/conditions/withdraw は protected で拒否する
    （note・violated・contradicted は出自・守りを問わず常に効く）。
    """
    if kind not in _ENTRY_KINDS:
        return _reject("invalid_spec", f"未知のentry kind: {kind}")

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_mode_not_off(conn)

        lesson_row = conn.execute(
            "SELECT id, kind FROM lessons WHERE handle = ?", (handle,)
        ).fetchone()
        if lesson_row is None:
            raise _Rejected(_reject("unknown_lesson", f"handleが無い: {handle}"))
        lesson_id = lesson_row["id"]

        current = conn.execute(
            "SELECT retracted, deliver_event FROM lesson_current WHERE lesson_id = ?", (lesson_id,)
        ).fetchone()
        if current["retracted"]:
            raise _Rejected(_reject("unknown_lesson", f"撤回済み: {handle}"))

        is_protected = conn.execute(
            "SELECT 1 FROM lesson_protected WHERE lesson_id = ?", (lesson_id,)
        ).fetchone() is not None
        if kind in _PROTECTED_BLOCKS and is_protected:
            raise _Rejected(_reject("protected", f"守られた知見への{kind}は拒否される: {handle}"))

        norm_deliver_event = deliver_event
        norm_deliver_spec = deliver_spec
        norm_step_event = step_event
        norm_step_spec = step_spec
        if kind == "conditions":
            shape = _lesson_kind_shape(conn, lesson_row["kind"])
            try:
                norm_deliver_event, norm_deliver_spec = _normalize_spec_pair(deliver_event, deliver_spec)
                norm_step_event, norm_step_spec = _normalize_spec_pair(step_event, step_spec)
            except (ValueError, KeyError, TypeError, re.error) as e:
                raise _Rejected(_reject("invalid_spec", str(e))) from e
            if not _kind_shape_matches(shape, norm_deliver_event, norm_step_event):
                raise _Rejected(_reject("kind_mismatch", f"{lesson_row['kind']} の条件の有無が合わない"))
            if norm_deliver_event == "session":
                lesson_body = conn.execute(
                    "SELECT body FROM lesson_current WHERE lesson_id = ?", (lesson_id,)
                ).fetchone()["body"]
                _check_session_pool(conn, lesson_id, lesson_body)
        elif kind == "body" and current["deliver_event"] == "session":
            _check_session_pool(conn, lesson_id, body or "")

        try:
            cur = conn.execute(
                """
                INSERT INTO lesson_entries (lesson_id, kind, body, note, quote,
                                             deliver_event, deliver_spec, step_event, step_spec)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    lesson_id, kind,
                    body if kind == "body" else None,
                    note if kind == "note" else None,
                    quote,
                    norm_deliver_event if kind == "conditions" else None,
                    norm_deliver_spec if kind == "conditions" else None,
                    norm_step_event if kind == "conditions" else None,
                    norm_step_spec if kind == "conditions" else None,
                ),
            )
        except sqlite3.IntegrityError as e:
            raise _Rejected(_db_error_to_reject(e)) from e

        entry_id = cur.lastrowid
        conn.commit()
        return {
            "ok": True, "handle": handle, "lesson_id": lesson_id,
            "entry_id": entry_id, "entry_kind": kind, "warnings": [],
        }
    except _Rejected as r:
        conn.rollback()
        return r.result
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return _reject("db_rejected", str(e))
    finally:
        conn.close()


_QUOTE_DISPLAY_MAX = 150


def get_lessons(
    handle: str | None = None,
    query: str | None = None,
    include_retired: bool = False,
    limit: int = 5,
) -> dict:
    """知見を引く（pullの配達）。どの vessel_meta.mode でも動く。

    1件の形には現在の本文・条件・出自・守られているか・引用・最新の補足・
    採点(X/B/M/U/U_budget/U_same/S/C/T)・引っ込み(retired)を含める。守られた
    知見には、そのまま打てる撤回の1行を添える。
    """
    limit = max(1, min(int(limit or 5), 10))
    conn = get_connection()
    try:
        base_sql = """
            SELECT lc.lesson_id, lc.handle, lc.kind, lc.body,
                   lc.deliver_event, lc.deliver_spec, lc.step_event, lc.step_spec,
                   lc.quote, lc.note,
                   lo.origin,
                   CASE WHEN lp.lesson_id IS NOT NULL THEN 1 ELSE 0 END AS protected,
                   lk.delivers,
                   ls.x, ls.b, ls.m, ls.u, ls.u_budget, ls.u_same, ls.s, ls.c, ls.t, ls.retired
            FROM lesson_current lc
            JOIN lesson_origin lo ON lo.lesson_id = lc.lesson_id
            JOIN lesson_kinds lk ON lk.kind = lc.kind
            JOIN lesson_score ls ON ls.lesson_id = lc.lesson_id
            LEFT JOIN lesson_protected lp ON lp.lesson_id = lc.lesson_id
        """
        params: list = []
        where = ["lc.retracted = 0"]
        if not include_retired:
            where.append("ls.retired = 0")

        if handle:
            where.append("lc.handle = ?")
            params.append(handle)
            sql = base_sql + " WHERE " + " AND ".join(where) + " LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        elif query:
            fts_sql = base_sql.replace(
                "FROM lesson_current lc",
                "FROM lessons_fts JOIN lessons l ON l.id = lessons_fts.rowid "
                "JOIN lesson_current lc ON lc.lesson_id = l.id",
            )
            where.insert(0, "lessons_fts MATCH ?")
            words = re.findall(r"\S+", query) or [query]
            params.append(" OR ".join(_escape_fts5_query(w) for w in words))
            sql = fts_sql + " WHERE " + " AND ".join(where) + " ORDER BY bm25(lessons_fts) LIMIT ?"
            params.append(limit)
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                rows = []
        else:
            sql = base_sql + " WHERE " + " AND ".join(where) + " ORDER BY lc.lesson_id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()

        items = []
        delivered_handles = []
        for r in rows:
            item = {
                "handle": r["handle"],
                "kind": r["kind"],
                "origin": r["origin"],
                "protected": bool(r["protected"]),
                "body": r["body"],
                "deliver_event": r["deliver_event"],
                "deliver_spec": r["deliver_spec"],
                "step_event": r["step_event"],
                "step_spec": r["step_spec"],
                "quote": (r["quote"] or "")[:_QUOTE_DISPLAY_MAX] or None,
                "note": r["note"],
                "score": {
                    "x": r["x"], "b": r["b"], "m": r["m"], "u": r["u"],
                    "u_budget": r["u_budget"], "u_same": r["u_same"],
                    "s": r["s"], "c": r["c"], "t": r["t"],
                },
                "retired": bool(r["retired"]),
            }
            if item["protected"]:
                item["withdraw_line"] = f"知見撤回 {r['handle']}"
            items.append(item)
            if r["delivers"] and not r["retired"]:
                delivered_handles.append({"handle": r["handle"], "body": r["body"]})

        return {"ok": True, "items": items, "delivered_handles": delivered_handles}
    finally:
        conn.close()
