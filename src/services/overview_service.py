"""進行状況の窓 — 4節の集計を1回で返す読み取り専用サービス。

読み取り専用であり、DB への書き込みを一切行わない。特に
activity_service.get_activities が持つ「期限切れ snoozed の自動復活」は
本サービスに持ち込まない（窓を覗いた副作用で status が動くと、窓が
映しているものと DB の実態がずれる）。

get_overview 1回の呼び出しで開く sqlite 接続は計5本になる
（activities側の接続1本 + ask_service.get_asksを4回呼ぶことによる4本）。
awaiting_human節はopen側・回答済み未捌き(pending)側それぞれで「非メタ取得+
kind="meta"専用取得」の2回呼びを行うため、この節だけで4本になる（メタaskは
表示上限（limit）を超えて他のaskが多数存在していても必ず表示するため）。
with_conn版の分離はconn共有のためではなくテストでの単体呼び出しやすさが
目的で、この5本を1本に集約する最適化は行っていない。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.config import HEARTBEAT_TIMEOUT_MINUTES
from src.db import get_connection, row_to_dict
from src.services import ask_service
from src.services.activity_service import REAL_STATUSES
from src.services.readable_id import strip_entity_id_inplace
from src.services.tag_service import get_entity_tags_batch

DEFAULT_DAYS = 7
DEFAULT_LIMIT = 20

# get_overview の limit 引数の上限。ask_service.get_asks の _MAX_LIMIT と揃える
# （第3節がask_service.get_asksへそのまま委譲するため、揃えないと節ごとに
# 切り詰め件数が食い違う。詳細はget_overviewのdocstring参照）。
_MAX_LIMIT = 100

# 鮮度の基準時刻。COALESCE を外すと SQLite の max() が NULL 伝播し、
# heartbeat を一度も打っていないアクティビティの last_touch_at が NULL になる。
_LAST_TOUCH = "MAX(a.updated_at, COALESCE(a.last_heartbeat_at, ''))"

# 'now'リテラルではなく:nowバインドパラメータを使う。呼び出し元がPythonの
# datetime.now(timezone.utc)から作った文字列をここへ流し込むことで、テストが
# 基準時刻をモックで固定できる（SQLiteの'now'はシステム時計を直接読むため
# Python側のモックが効かない）。
#
# COALESCE を外すと last_heartbeat_at IS NULL の行でこの式が NULL になり、
# 第4節の NOT (_HOT) が NULL となって行が両方の節から消える。
_IS_LIVE = "(COALESCE(a.last_heartbeat_at, '') > datetime(:now, '-' || :hb_min || ' minutes'))"

_HOT = f"""(
    {_IS_LIVE}
    OR (a.status = 'in_progress'
        AND {_LAST_TOUCH} >= datetime(:now, '-' || :days || ' days'))
)"""


def _sql_in_list(values) -> str:
    """固定のstatus定数集合からSQLのIN句用リテラル列を組み立てる。

    値はactivity_service側の定数（コード内の固定集合であり、呼び出し引数や
    DB内容など外部由来の値ではない）に限定して使う。文字列連結だが注入経路にはならない。
    """
    return ", ".join(f"'{v}'" for v in sorted(values))


# workingに載りうるstatus。activity_service.ACTIVE_STATUSESは
# get_activitiesのstatus="active"フィルタ別名の定義であり別概念のため、
# そちらには結びつけない（statusの全体集合はREAL_STATUSES）。
# 「pendingが混ざりうるのは_IS_LIVE側の分岐のみ（_HOTの2つ目の分岐は
# status='in_progress'を要求するため、pending行はheartbeatが生きている
# 場合にしか_HOTを満たさない）」。
_WORKING_ELIGIBLE_STATUSES = ("in_progress", "pending")
_WORKING_ELIGIBLE_STATUS = f"a.status IN ({_sql_in_list(_WORKING_ELIGIBLE_STATUSES)})"

_WORKING_WHERE = f"""
    {_WORKING_ELIGIBLE_STATUS}
    AND {_HOT}
"""

# backlog節の母集団条件。「未完了で、working節に載らなかったもの」。
# statusはactivity_service.REAL_STATUSESから'completed'を除いたもの
# （手書きリテラルにすると、将来REAL_STATUSESに新statusが増えても
# ここが追随せず4節すべてから静かに漏れる）。
# 除外側は_HOT単体ではなく「working対象statusかつ_HOT」の否定にする。
# snoozed/shelvedはそもそもworkingのstatus条件を満たさないため、この2状態は
# heartbeatの生死に関わらず常にbacklog側に残る（_HOT単体をNOTすると、
# heartbeatだけ生きているsnoozed/shelvedがworking・backlogどちらの
# 条件も満たさず消えてしまう）。
_BACKLOG_STATUSES = REAL_STATUSES - {"completed"}
_BACKLOG_WHERE = f"""
    a.status IN ({_sql_in_list(_BACKLOG_STATUSES)})
    AND NOT ({_WORKING_ELIGIBLE_STATUS} AND {_HOT})
"""


def _invalid_parameter(message: str) -> dict:
    return {"error": {"code": "INVALID_PARAMETER", "message": message}}


def _extract_domains(tags: list[str]) -> list[str]:
    """タグ文字列リストから domain: 名前空間の name 部分だけを抜き出す。"""
    return [tag.split(":", 1)[1] for tag in tags if tag.startswith("domain:")]


def _days_since(timestamp_str: str, now: datetime) -> int:
    """`YYYY-MM-DD HH:MM:SS`（UTC）文字列から now までの経過日数を計算する。

    hooks/session_start_hook.py の _calc_elapsed_days と同じ床関数の意味になる
    （naive文字列にUTCを付与してnowとの差を取り、timedelta.daysで切り捨てる）。
    now は呼び出し元から渡す。get_overview 内で二度目の datetime.now() を
    読むと、generated_at・SQL側の:nowバインドと基準時刻がずれうるため
    （日跨ぎの瞬間に発生しうる不整合を避ける）。
    """
    ts = datetime.fromisoformat(timestamp_str).replace(tzinfo=timezone.utc)
    return (now - ts).days


def _rows_to_items(conn: sqlite3.Connection, rows: list[sqlite3.Row], *, bool_fields: tuple[str, ...] = ()) -> list[dict]:
    """activitiesのrowsからitem配列を組み立てる（working/recently_done共通)。

    id集めてdomainsをバッチ合流し、id_rawへ正規化するまでの並びを1箇所にまとめる。
    bool_fieldsで指定した列はSQLite上0/1の整数で返るためboolへ変換する。
    """
    ids = [row["id"] for row in rows]
    tags_map = get_entity_tags_batch(conn, "activity_tags", "activity_id", ids)

    items = []
    for row in rows:
        item = row_to_dict(row)
        item["domains"] = _extract_domains(tags_map.get(item["id"], []))
        for field in bool_fields:
            item[field] = bool(item[field])
        strip_entity_id_inplace(item)
        items.append(item)
    return items


def _collect_working_with_conn(conn: sqlite3.Connection, *, days: int, limit: int, hb_min: int, now: str) -> dict:
    """working節（今動いているもの）を集計する。

    heartbeat が生きているか、in_progress かつ max(updated_at, heartbeat) が
    days 日以内のアクティビティを対象にする。status='in_progress' は宣言で
    あって実態ではないため、鮮度を掛けたものだけを載せる。
    """
    params = {"days": days, "hb_min": hb_min, "now": now}

    total_count = conn.execute(
        f"SELECT COUNT(*) FROM activities a WHERE {_WORKING_WHERE}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT a.id, a.title, a.status,
               {_LAST_TOUCH} AS last_touch_at,
               CASE WHEN {_IS_LIVE} THEN 1 ELSE 0 END AS is_live,
               CAST(julianday(:now) - julianday({_LAST_TOUCH}) AS INTEGER) AS days_since_touch,
               (SELECT COUNT(*) FROM ask_blocks ab
                 JOIN asks k ON k.id = ab.ask_id
                WHERE ab.activity_id = a.id AND k.status = 'open') AS open_ask_count
        FROM activities a
        WHERE {_WORKING_WHERE}
        ORDER BY is_live DESC, last_touch_at DESC
        LIMIT :limit
        """,
        {**params, "limit": limit},
    ).fetchall()

    items = _rows_to_items(conn, rows, bool_fields=("is_live",))

    return {"items": items, "count": len(items), "total_count": total_count}


def _collect_recently_done_with_conn(conn: sqlite3.Connection, *, days: int, limit: int, now: str) -> dict:
    """recently_done節（最近終わったもの）を集計する。

    completedかつupdated_atがdays日以内のアクティビティが対象。days日より
    古いcompletedはこの節にもbacklogにも現れない（仕様。backlog母集団は
    完了系statusを含まないため）。activitiesに完了時刻カラムは無いため、
    完了日時はupdated_atで近似する（caveat: 完了済みアクティビティを後から
    編集するとupdated_atがbumpされ再浮上する。件数を「今週の完了数」として
    語らないこと）。
    """
    where = "a.status = 'completed' AND a.updated_at >= datetime(:now, '-' || :days || ' days')"
    params = {"days": days, "now": now}

    total_count = conn.execute(
        f"SELECT COUNT(*) FROM activities a WHERE {where}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT a.id, a.title, a.status, a.updated_at,
               CAST(julianday(:now) - julianday(a.updated_at) AS INTEGER) AS days_ago
        FROM activities a
        WHERE {where}
        ORDER BY a.updated_at DESC, a.id DESC
        LIMIT :limit
        """,
        {**params, "limit": limit},
    ).fetchall()

    items = _rows_to_items(conn, rows)

    return {"items": items, "count": len(items), "total_count": total_count}


def _collect_backlog_with_conn(conn: sqlite3.Connection, *, days: int, hb_min: int, now: str) -> dict:
    """backlog節（それ以外の残り）を集計する。件数と内訳のみで個別アクティビティは返さない。

    集計軸はdomain:タグ。tag-cleanupでdomainタグを統合すると別名と正規名に票が
    割れるため、canonical解決してから数える（get_active_domains_with_connは
    canonical解決していないが、本ツールだけ意図的に解決する）。
    """
    params = {"days": days, "hb_min": hb_min, "now": now}

    total_count = conn.execute(
        f"SELECT COUNT(*) FROM activities a WHERE {_BACKLOG_WHERE}", params
    ).fetchone()[0]

    by_status: dict[str, int] = {}
    for row in conn.execute(
        f"SELECT a.status, COUNT(*) AS c FROM activities a WHERE {_BACKLOG_WHERE} GROUP BY a.status",
        params,
    ).fetchall():
        by_status[row["status"]] = row["c"]

    by_domain_rows = conn.execute(
        f"""
        SELECT COALESCE(ct.name, t.name) AS domain, COUNT(DISTINCT a.id) AS c
        FROM activities a
        JOIN activity_tags at ON at.activity_id = a.id
        JOIN tags t          ON t.id = at.tag_id
        LEFT JOIN tags ct    ON ct.id = t.canonical_id
        WHERE {_BACKLOG_WHERE}
          AND COALESCE(ct.namespace, t.namespace) = 'domain'
        GROUP BY domain
        ORDER BY c DESC, domain ASC
        """,
        params,
    ).fetchall()
    by_domain = [{"domain": row["domain"], "count": row["c"]} for row in by_domain_rows]

    no_domain_count = conn.execute(
        f"""
        SELECT COUNT(*) FROM activities a
        WHERE {_BACKLOG_WHERE}
          AND NOT EXISTS (
            SELECT 1 FROM activity_tags at
            JOIN tags t       ON t.id = at.tag_id
            LEFT JOIN tags ct ON ct.id = t.canonical_id
            WHERE at.activity_id = a.id
              AND COALESCE(ct.namespace, t.namespace) = 'domain')
        """,
        params,
    ).fetchone()[0]

    return {
        "total_count": total_count,
        "stale_in_progress_count": by_status.get("in_progress", 0),
        "by_status": by_status,
        "by_domain": by_domain,
        "no_domain_count": no_domain_count,
    }


def _fetch_asks_with_guaranteed_meta(*, non_meta_limit: int, **base_kwargs) -> dict:
    """base_kwargsに合致するaskを取得し、kind="meta"のものは非メタの表示上限
    （non_meta_limit）に関わらず必ず含める（メタask常時表示）。

    base_kwargsはask_service.get_asksへそのまま渡すフィルタ（status="open"や
    triage_pending_only=True等）。kindは指定しない（既存の全kind混在取得を
    そのまま使う）。メタask専用に_MAX_LIMIT件までの取得を別途行い、idで
    dedupしてから先頭に配置する。

    total_countは非メタ側の呼び出しが返すtotal_count（kindで絞っていない
    SELECT COUNT(*)であり、既にメタも含む母集団全体の件数）のみを使う。
    メタ専用取得側の件数をここへ加算しない。加算すると、非メタ側のページ
    （non_meta_limit件）にメタが偶然含まれるかどうかでtotal_countが
    non_meta_limitの値ごとに変動してしまい、「件数はlimitに依らない」という
    既存の設計思想（triage_pending_countの元々の実装意図）を壊す。
    """
    non_meta = ask_service.get_asks(limit=non_meta_limit, **base_kwargs)
    if "error" in non_meta:
        return non_meta
    meta = ask_service.get_asks(kind="meta", limit=_MAX_LIMIT, **base_kwargs)
    if "error" in meta:
        return meta

    meta_ids = {ask["id_raw"] for ask in meta["asks"]}
    deduped_non_meta = [ask for ask in non_meta["asks"] if ask["id_raw"] not in meta_ids]
    asks = meta["asks"] + deduped_non_meta  # メタを先頭に

    return {"asks": asks, "total_count": non_meta["total_count"]}


def _format_ask_item(ask: dict, now: datetime) -> dict:
    """ask_service.get_asksが返す1件から、awaiting_human表示用のitemを組み立てる。

    items・triage_pending_items共通の整形ロジック（両方とも同じask形状を
    受け取るため）。
    """
    first_seen_at = ask["first_seen_at"]
    return {
        "id_raw": ask["id_raw"],
        "question": ask["question"],
        "kind": ask["kind"],
        "choices": ask.get("choices"),
        "occurrence_count": ask["occurrence_count"],
        "first_seen_at": first_seen_at,
        "days_open": _days_since(first_seen_at, now),
        "domains": _extract_domains(ask.get("tags", [])),
        "blocks": ask.get("blocks", []),
    }


def _collect_awaiting_human(*, limit: int, now: datetime) -> dict:
    """awaiting_human節（人間の裁定待ちで止まっているもの）を集計する。

    抽出条件はstatus='open'。status='answered' AND triage IS NULL
    （トリアージ未了）はtriage_pending_countとして件数を返すのに加え、
    triage_pending_itemsとしてタイトル（question）付きの一覧も返す
    （件数だけでは何を捌くべきか分からず、人間が既に答えた内容を思い出す
    手段がないため）。残っているのはpromote/dismissという処理側の仕事だが、
    その判断材料としてquestion等を見せる。

    kind="meta"のaskは、items・triage_pending_itemsいずれにおいても表示上限
    （limit）を超えて他のaskが多数存在していても必ず含め、両配列内で非メタ
    askより先頭に配置する（_fetch_asks_with_guaranteed_meta参照）。

    kindフィールドは"ask"/"meta"の固定enumとして型付けしない（将来"decision"
    が増える設計変更を型定義の変更なしに受け入れるため）。
    """
    raw = _fetch_asks_with_guaranteed_meta(non_meta_limit=limit, status="open")
    if "error" in raw:
        return raw
    pending = _fetch_asks_with_guaranteed_meta(
        non_meta_limit=limit, status=None, triage_pending_only=True
    )
    if "error" in pending:
        return pending

    items = [_format_ask_item(ask, now) for ask in raw["asks"]]
    triage_pending_items = [_format_ask_item(ask, now) for ask in pending["asks"]]

    return {
        "items": items,
        "count": len(items),
        "total_count": raw["total_count"],
        "triage_pending_count": pending["total_count"],
        "triage_pending_items": triage_pending_items,
    }


def get_overview(days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT) -> dict:
    """4節（working / recently_done / awaiting_human / backlog）を集計して返す。

    副作用ゼロ。activity_service.get_activitiesが持つ「期限切れsnoozedの
    自動復活」は行わない。

    limitは入口で上限側のみ100に丸める（ask_service.get_asksが内部で黙って
    100に切るため、丸めないとawaiting_human節だけ他節と件数が食い違う）。
    limit<1・days<1は丸めずにINVALID_PARAMETERで弾く（activity_service.
    get_activitiesの既存挙動に揃える）。heartbeatのタイムアウトは引数にせず、
    src.config.HEARTBEAT_TIMEOUT_MINUTESの実効値をそのまま使う。

    Returns:
        成功時: {"generated_at", "params", "working", "recently_done",
                 "awaiting_human", "backlog"}
        失敗時: {"error": {"code": "INVALID_PARAMETER" | "DATABASE_ERROR", "message": str}}
    """
    if days < 1:
        return _invalid_parameter(f"days must be positive, got {days}")
    if limit < 1:
        return _invalid_parameter(f"limit must be positive, got {limit}")
    limit = min(limit, _MAX_LIMIT)

    hb_min = HEARTBEAT_TIMEOUT_MINUTES
    # generated_at・SQL側の:nowバインド・awaiting_human.items[].days_openを
    # 同じ基準時刻にする。個別にdatetime.now()するとその間の実時間経過分だけ
    # 理論上ずれうるため、1回だけ計算して使い回す（now_strはSQL・generated_at用、
    # nowはPython側でdays_openを計算する_days_since用）。
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection()
    try:
        working = _collect_working_with_conn(conn, days=days, limit=limit, hb_min=hb_min, now=now_str)
        recently_done = _collect_recently_done_with_conn(conn, days=days, limit=limit, now=now_str)
        backlog = _collect_backlog_with_conn(conn, days=days, hb_min=hb_min, now=now_str)
    except Exception as e:
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()

    try:
        awaiting_human = _collect_awaiting_human(limit=limit, now=now)
    except Exception as e:
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    if "error" in awaiting_human:
        return awaiting_human

    return {
        "generated_at": now_str,
        "params": {"days": days, "limit": limit, "heartbeat_timeout_minutes": hb_min},
        "working": working,
        "recently_done": recently_done,
        "awaiting_human": awaiting_human,
        "backlog": backlog,
    }
