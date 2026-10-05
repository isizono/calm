"""運用計測の突合集計: 巻き戻し率・shadow乖離率・矛盾/miss/誤類推件数・goal観測・
search/precedent/fetch/citation telemetryの集計・guard_block件数

signal_events テーブル（記録先は品質投資コンポーネントの signal_service が正）、
search_telemetry/precedent_telemetry/fetch_telemetry/citation_event_log の各
telemetryテーブル、GO判定パッケージの機械可読ブロック（go_package.py extract /
shadow-report が出力するJSON。--packages-file で受ける）を読み、率指標を計算する、
この種の集計の唯一の実装体。生データを読むだけで、閾値判定・昇格判定は行わない。

Usage:
    uv run python scripts/ops_metrics.py [--window-days 30] [--db <path>] [--json]
                                          [--packages-file <json>]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# プロジェクトルートをパスに追加（src.db等の参照用）
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# 運用計測が読む signal_events の kind。9 種のうち machine_error / friction は
# 汎用の故障・不満報告であり率指標の対象外（品質投資コンポーネントの管轄）。
# goal_rollback（goal判定の差し戻し）は _goal_metrics が読む。_rollback_metrics は
# kind='rollback' を固定文字列で問い合わせるため、goal_rollback の追加で
# 巻き戻し率の分子が変わることはない。guard_block は _guard_block_metrics が読む。
_CONTRADICTION_RESOLUTIONS = ("existing_correct", "new_correct", "unresolved")

# boundary_case / rollback の context スキーマ（mode / machine_verdict / divergence の
# 許容値）を規定する唯一の箇所。生成側とこの定義がずれると、率指標の分母が 0 になり
# _rate() が黙って N/A を返すだけで計測破綻に気づけない（検知手段のない既知の制約）。
_BOUNDARY_MODE_LIVE = "live"
_BOUNDARY_MODE_SHADOW = "shadow"
_BOUNDARY_VERDICT_POST_VETO = "post_veto_candidate"
_DIVERGENCE_NONE = "none"
_DIVERGENCE_FALSE_NEGATIVE = "false_negative"

# report_signal 経由の手動呼び出し以外にこの kind を書く自動化コードが存在しない
# （grep で確認済み。signal_service.KNOWN_KINDS からは外さない）。これらを分子/分母に
# 使う率指標の 0 件は「インシデントが起きていない」のではなく「書き手が無く計測され
# ていない」という構造的な 0 であり、下記の各 _*_metrics はこの事実を no_writer_code
# フラグとして返り値に含める。
_NO_WRITER_CODE_KINDS = frozenset({"precedent_miss", "precedent_misapplied", "boundary_case", "rollback"})


def _connect(db_path: str) -> sqlite3.Connection:
    """読み取り専用の軽量接続を作る（sqlite-vec拡張のロードは不要なため素の sqlite3 を使う）。

    他の DB 接続経路（src.db.get_connection / sanitize hook）に揃えて busy_timeout を
    設定し、WAL チェックポイント等での一時ロックに即エラーを返さずリトライ待機させる。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _fetch_signals(conn: sqlite3.Connection, kind: str, window_days: int | None) -> list[dict]:
    """指定 kind の signal_events 行を取得し、context/refs を JSON パースして返す。

    window_days が None のときは全期間、指定時は first_seen_at が
    直近 window_days 日以内の行に絞る。同一案件の再報告は fingerprint dedup
    により1行に集約されている前提のため、ここでは行数=件数として扱う
    （occurrence_count は「同じ事象が何度報告されたか」であり「事象が何件起きたか」
    ではない）。
    """
    query = "SELECT * FROM signal_events WHERE kind = ?"
    params: list[object] = [kind]
    if window_days is not None:
        query += " AND first_seen_at >= datetime('now', ?)"
        params.append(f"-{window_days} days")

    rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["context"] = json.loads(d["context"]) if d.get("context") else {}
        d["refs"] = json.loads(d["refs"]) if d.get("refs") else None
        result.append(d)
    return result


def _rate(numerator: int, denominator: int) -> float | None:
    """denominator が 0 のとき None（N/A）を返し、ゼロ除算を避ける。"""
    if denominator == 0:
        return None
    return numerator / denominator


def _contradiction_metrics(conn: sqlite3.Connection, window_days: int | None) -> dict:
    """矛盾イベント数と resolution 内訳を返す。"""
    rows = _fetch_signals(conn, "contradiction", window_days)
    by_resolution = {res: 0 for res in _CONTRADICTION_RESOLUTIONS}
    by_resolution["unknown"] = 0
    for row in rows:
        resolution = (row["context"] or {}).get("resolution")
        if resolution in by_resolution:
            by_resolution[resolution] += 1
        else:
            by_resolution["unknown"] += 1
    return {"count": len(rows), "by_resolution": by_resolution}


def _rollback_metrics(
    conn: sqlite3.Connection,
    window_days: int | None,
    boundary_rows: list[dict],
) -> dict:
    """巻き戻し率 = rollback件数 / boundary_case(mode=live, machine_verdict=post_veto_candidate)件数。

    分子(rollback)と分母(boundary_case)は案件IDでリンクしておらず、それぞれ
    first_seen_at の window で独立に絞り込むだけである。境界案件の記録時点と
    rollback 時点が window 境界をまたぐと、分子が分母を上回るなど実態と乖離した
    値になり得る。案件IDによる突合は生成側（境界ゲート）が未実装のため、現状は
    独立カウントの近似値として扱う（既知の制約）。
    """
    rollback_rows = _fetch_signals(conn, "rollback", window_days)
    denom_rows = [
        row
        for row in boundary_rows
        if (row["context"] or {}).get("mode") == _BOUNDARY_MODE_LIVE
        and (row["context"] or {}).get("machine_verdict") == _BOUNDARY_VERDICT_POST_VETO
    ]
    numerator = len(rollback_rows)
    denominator = len(denom_rows)
    return {
        "rollback_count": numerator,
        "post_veto_live_count": denominator,
        "rate": _rate(numerator, denominator),
        "no_writer_code": "rollback" in _NO_WRITER_CODE_KINDS,
    }


def _shadow_divergence_metrics(boundary_rows: list[dict]) -> dict:
    """shadow乖離率 = boundary_case(mode=shadow)のうちdivergence!=noneの割合。false_negativeは別掲。"""
    shadow_rows = [
        row for row in boundary_rows if (row["context"] or {}).get("mode") == _BOUNDARY_MODE_SHADOW
    ]
    total = len(shadow_rows)
    diverged = [
        row
        for row in shadow_rows
        if (row["context"] or {}).get("divergence", _DIVERGENCE_NONE) != _DIVERGENCE_NONE
    ]
    false_negative = [
        row
        for row in shadow_rows
        if (row["context"] or {}).get("divergence") == _DIVERGENCE_FALSE_NEGATIVE
    ]
    return {
        "shadow_total": total,
        "diverged_count": len(diverged),
        "divergence_rate": _rate(len(diverged), total),
        "false_negative_count": len(false_negative),
        "false_negative_rate": _rate(len(false_negative), total),
        "no_writer_code": "boundary_case" in _NO_WRITER_CODE_KINDS,
    }


def _sum_citation_slots(packages: list[dict]) -> int:
    """--packages-file の各パッケージについて precedents 件数 + pull.presented 件数
    （presented がリストのときのみ。'unavailable' 等の文字列は対象外）を合算する。

    pull hit 率の分母（「判例引用が提示・引用された機会の総数」）として使う。
    """
    total = 0
    for package in packages:
        precedents = package.get("precedents") or []
        total += len(precedents)
        presented = (package.get("pull") or {}).get("presented")
        if isinstance(presented, list):
            total += len(presented)
    return total


def _count_applied_citations(packages: list[dict]) -> int:
    """--packages-file の precedents のうち stance=applied の件数を合算する（誤類推率の分母）。"""
    total = 0
    for package in packages:
        for precedent in package.get("precedents") or []:
            if precedent.get("stance") == "applied":
                total += 1
    return total


def _pull_metrics(conn: sqlite3.Connection, window_days: int | None, packages: list[dict] | None) -> dict:
    """pull miss 件数 / hit率。--packages-file 未供給時は件数のみ返す。"""
    miss_rows = _fetch_signals(conn, "precedent_miss", window_days)
    result: dict = {"miss_count": len(miss_rows), "no_writer_code": "precedent_miss" in _NO_WRITER_CODE_KINDS}
    if packages is not None:
        denominator = _sum_citation_slots(packages)
        result["citation_slot_count"] = denominator
        result["miss_rate"] = _rate(len(miss_rows), denominator)
    return result


def _misapplied_metrics(conn: sqlite3.Connection, window_days: int | None, packages: list[dict] | None) -> dict:
    """誤類推件数 / 誤類推率。--packages-file 未供給時は件数のみ返す。"""
    misapplied_rows = _fetch_signals(conn, "precedent_misapplied", window_days)
    result: dict = {
        "misapplied_count": len(misapplied_rows),
        "no_writer_code": "precedent_misapplied" in _NO_WRITER_CODE_KINDS,
    }
    if packages is not None:
        denominator = _count_applied_citations(packages)
        result["applied_citation_count"] = denominator
        result["misapplied_rate"] = _rate(len(misapplied_rows), denominator)
    return result


def _goal_tables_exist(conn: sqlite3.Connection) -> bool:
    """goal機構の3表（goals/goal_conditions/goal_activities）がこのDBにあるか。

    migration 0077 未適用のDBでも運用計測全体が落ちないよう、goal観測だけを
    飛ばせるようにするための判定。
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM sqlite_master
        WHERE type = 'table' AND name IN ('goals', 'goal_conditions', 'goal_activities')
        """
    ).fetchone()
    return row["c"] == 3


def _goal_metrics(conn: sqlite3.Connection, window_days: int | None) -> dict | None:
    """goal機構の観測: 差し戻し回数・判定件数・誤判定率・放置件数。

    goal は活動と異なり時系列のイベントログではなく現在の状態そのもの
    （goals.closed）だが、誤判定率の分子（差し戻し件数）と分母（判定件数）を
    同じ期間で揃えるため、判定件数もgoals.judged_atでwindowを絞る。放置件数は
    「いま」の状態をそのまま数える。goal機構の3表が無いDB（migration未適用）
    ではNoneを返す。

    既知の制約: 分子と分母はgoal IDでリンクしておらず、それぞれwindow内で
    独立に絞り込むだけである。window開始前に判定されたgoalがwindow内で
    巻き戻された場合、差し戻しは分子に入るが判定はwindow外で分母に入らない
    ため、誤判定率が1を超えることがある。
    """
    if not _goal_tables_exist(conn):
        return None

    rollback_count = len(_fetch_signals(conn, "goal_rollback", window_days))

    closed_query = "SELECT COUNT(*) AS c FROM goals WHERE closed = 1"
    closed_params: list[object] = []
    if window_days is not None:
        closed_query += " AND judged_at >= datetime('now', ?)"
        closed_params.append(f"-{window_days} days")
    closed_count = conn.execute(closed_query, closed_params).fetchone()["c"]

    judged_count = closed_count + rollback_count

    # 放置 = 未判定(closed=0)のまま、紐づくactivityが全部completed。
    neglected_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM goals g
        WHERE g.closed = 0
          AND NOT EXISTS (
              SELECT 1 FROM goal_activities ga
              JOIN activities a ON a.id = ga.activity_id
              WHERE ga.goal_id = g.id AND a.status <> 'completed'
          )
        """
    ).fetchone()["c"]

    return {
        "rollback_count": rollback_count,
        "judged_count": judged_count,
        "misjudgment_rate": _rate(rollback_count, judged_count),
        "neglected_count": neglected_count,
    }


def _fetch_rows(conn: sqlite3.Connection, table: str, timestamp_col: str, window_days: Optional[int]) -> list[dict]:
    """telemetryテーブルの全カラムをdictの一覧で返す（signal_eventsのcontext/refsのような
    JSONカラムのパースは呼び出し側に委ねる。テーブルごとにパース対象カラムが異なるため）。
    """
    query = f"SELECT * FROM {table}"
    params: list[object] = []
    if window_days is not None:
        query += f" WHERE {timestamp_col} >= datetime('now', ?)"
        params.append(f"-{window_days} days")
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _search_telemetry_metrics(conn: sqlite3.Connection, window_days: Optional[int]) -> dict:
    """search_telemetry の縮退率（ベクトル検索利用不可率）とクエリ拡張発火率。

    diagnostics_json（migration 0054以降のみ記録、旧行はNULL）が無い行は
    どちらの分母にも入れない。diagnostics_countがtotal_countより小さいのは
    migration 0054以前の旧行が window 内に残っている場合の既知の挙動。
    """
    rows = _fetch_rows(conn, "search_telemetry", "timestamp", window_days)
    degraded = 0
    qe_fired = 0
    diagnostics_count = 0
    for row in rows:
        raw = row.get("diagnostics_json")
        if not raw:
            continue
        try:
            diag = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        diagnostics_count += 1
        if diag.get("degraded"):
            degraded += 1
        if diag.get("qe_expansions"):
            qe_fired += 1
    return {
        "total_count": len(rows),
        "diagnostics_count": diagnostics_count,
        "degraded_count": degraded,
        "degraded_rate": _rate(degraded, diagnostics_count),
        "qe_fired_count": qe_fired,
        "qe_fire_rate": _rate(qe_fired, diagnostics_count),
    }


_PRECEDENT_GUARANTEES = ("enumerated", "routing_miss", "routing_unavailable")


def _precedent_telemetry_metrics(conn: sqlite3.Connection, window_days: Optional[int]) -> dict:
    """precedent_telemetry のguarantee内訳（routingの当たり外れ）と列挙カバレッジ。

    カバレッジ(full_count/decisions_total)はguarantee=enumeratedかつdecisions_total>0の
    行だけを対象にした単純平均（呼出ごとの重みは均等、decisions_total自体の大小では
    重み付けしない）。
    """
    rows = _fetch_rows(conn, "precedent_telemetry", "timestamp", window_days)
    by_guarantee = {g: 0 for g in _PRECEDENT_GUARANTEES}
    by_guarantee["unknown"] = 0
    coverage_ratios: list[float] = []
    for row in rows:
        guarantee = row.get("guarantee")
        if guarantee in by_guarantee:
            by_guarantee[guarantee] += 1
        else:
            by_guarantee["unknown"] += 1
        decisions_total = row.get("decisions_total") or 0
        if guarantee == "enumerated" and decisions_total > 0:
            coverage_ratios.append((row.get("full_count") or 0) / decisions_total)
    avg_coverage = sum(coverage_ratios) / len(coverage_ratios) if coverage_ratios else None
    return {
        "count": len(rows),
        "by_guarantee": by_guarantee,
        "enumerated_full_coverage_rate": avg_coverage,
    }


def _fetch_follow_metrics(conn: sqlite3.Connection, window_days: Optional[int]) -> dict:
    """search_telemetry の検索結果が同一セッションの fetch_telemetry で後から取得された
    割合（追随率）。migrations/0054 が定める生データの意図通り、caller_session_id で
    post-hocにJOINする。

    既知の近似: (1) fetch側の集合は項目単位(type,id)の後方一致判定に使うだけで、
    どのfetch呼出がどの検索由来かは案件単位でリンクしない。(2) 同一(type,id)が
    複数回検索結果に出た場合はfollowed判定も複数回加算される（検索結果アイテム
    単位の集計であり、ユニークなアイテム単位ではない）。caller_session_idが
    NULLの行（MCP context外の直接呼出）はどちらの側も対象外にする。
    """
    search_rows = _fetch_rows(conn, "search_telemetry", "timestamp", window_days)
    fetch_rows = _fetch_rows(conn, "fetch_telemetry", "timestamp", window_days)

    # session単位: (type, id) -> そのitemが最後にfetchされた時刻(文字列比較で十分な
    # ISO風フォーマット)。「このitemが検索時刻以降にfetchされたことがあるか」は
    # 最後のfetch時刻が検索時刻以降かどうかと同値（最後のfetchより前の時刻は
    # 全て検索時刻以降ではあり得ないため）。最初のfetch時刻を使うと、検索より前に
    # 1回fetchされた後、検索後に再fetchされたケースを後続と判定できない。
    fetched_at_by_session: dict[str, dict[tuple, str]] = {}
    for row in fetch_rows:
        session_id = row.get("caller_session_id")
        if not session_id:
            continue
        try:
            items = json.loads(row["items_json"])
        except (TypeError, json.JSONDecodeError, KeyError):
            continue
        ts = row.get("timestamp") or ""
        bucket = fetched_at_by_session.setdefault(session_id, {})
        for item in items:
            if not isinstance(item, dict) or item.get("type") is None or item.get("id") is None:
                continue
            key = (item["type"], item["id"])
            if key not in bucket or ts > bucket[key]:
                bucket[key] = ts

    total_items = 0
    followed_items = 0
    for row in search_rows:
        session_id = row.get("caller_session_id")
        raw_results = row.get("results_json")
        if not session_id or not raw_results:
            continue
        try:
            results = json.loads(raw_results)
        except (TypeError, json.JSONDecodeError):
            continue
        search_ts = row.get("timestamp") or ""
        bucket = fetched_at_by_session.get(session_id, {})
        for item in results:
            if not isinstance(item, dict) or item.get("type") is None or item.get("id") is None:
                continue
            total_items += 1
            fetched_at = bucket.get((item["type"], item["id"]))
            if fetched_at is not None and fetched_at >= search_ts:
                followed_items += 1

    return {
        "search_result_count": total_items,
        "followed_count": followed_items,
        "follow_rate": _rate(followed_items, total_items),
    }


_CITATION_VERIFICATION_RESULTS = ("exists", "dangling", "skip")


def _citation_event_log_metrics(conn: sqlite3.Connection, window_days: Optional[int]) -> dict:
    """citation_event_log の検証結果（verification_result）内訳。

    verification_resultは`{{cite:X#NNN}}`参照先の存在確認結果。NULL（未検証、
    write_auto_convert/bulk_migration等の変換自体にはverificationが伴わない行）は
    not_verifiedにまとめる。
    """
    rows = _fetch_rows(conn, "citation_event_log", "occurred_at", window_days)
    by_result = {r: 0 for r in _CITATION_VERIFICATION_RESULTS}
    by_result["not_verified"] = 0
    for row in rows:
        result = row.get("verification_result")
        if result in by_result:
            by_result[result] += 1
        else:
            by_result["not_verified"] += 1
    return {"count": len(rows), "by_verification_result": by_result}


def _guard_block_metrics(conn: sqlite3.Connection, window_days: Optional[int]) -> dict:
    """hookのdeny判定が記録したguard_block signalの件数を(hook, 規則)単位で集計する。

    signal_eventsのdedupは同一(kind, source, summary)の再発をoccurrence_count加算
    で1行に畳むため、行数ではなくSUM(occurrence_count)を件数として数える。
    last_seen_atでwindowを絞る（first_seen_atだと、window開始前に初回発生し以後も
    頻発し続けている行が丸ごと分母から落ちる）。件数は該当行がトリアージされず
    'new'のまま残る限り積み上がる累積値であり、「直近N日に増えた回数」ではない。
    """
    query = (
        "SELECT source, summary, SUM(occurrence_count) AS c FROM signal_events "
        "WHERE kind = 'guard_block'"
    )
    params: list[object] = []
    if window_days is not None:
        query += " AND last_seen_at >= datetime('now', ?)"
        params.append(f"-{window_days} days")
    query += " GROUP BY source, summary"
    rows = conn.execute(query, params).fetchall()
    by_rule = [
        {"source": row["source"], "summary": row["summary"], "count": row["c"]} for row in rows
    ]
    return {"total_count": sum(r["count"] for r in by_rule), "by_rule": by_rule}


def compute_metrics(
    db_path: str,
    window_days: int | None = 30,
    packages: list[dict] | None = None,
) -> dict:
    """signal_events (+ 供給時は packages) を読み、率指標の突合集計結果を返す。

    Args:
        db_path: signal_events を含む SQLite DB のパス
        window_days: 集計対象期間（日数）。None のとき全期間を対象にする
        packages: go_package.py extract/shadow-report が出力する go-package
            機械可読ブロックの一覧（パース済み）。None のとき pull hit率・誤類推率は
            件数のみ返し、率は計算しない

    Returns:
        矛盾イベント数・巻き戻し率・shadow乖離率・pull miss・誤類推・goal観測・
        search/precedent/fetch/citation telemetryの集計・guard_block件数の結果。
        goal観測はgoal機構の3表が無いDBではNone。telemetryテーブル自体が無いDB
        （未マイグレーション）ではKeyError/sqlite3.OperationalErrorが伝播する
        （goalのような存在チェックは行わない。これらは0041/0046/0051/0054という
        本体より古いmigrationが入れたテーブルで、運用DBに無いことは想定しない）
    """
    conn = _connect(db_path)
    try:
        # rollback率と shadow乖離率はどちらも同一 kind・同一 window の boundary_case を
        # 参照するため、ここで一度だけ取得して両者へ渡す。
        boundary_rows = _fetch_signals(conn, "boundary_case", window_days)
        return {
            "window_days": window_days,
            "contradiction": _contradiction_metrics(conn, window_days),
            "rollback": _rollback_metrics(conn, window_days, boundary_rows),
            "shadow_divergence": _shadow_divergence_metrics(boundary_rows),
            "pull": _pull_metrics(conn, window_days, packages),
            "precedent_misapplied": _misapplied_metrics(conn, window_days, packages),
            "goal": _goal_metrics(conn, window_days),
            "search_telemetry": _search_telemetry_metrics(conn, window_days),
            "precedent_telemetry": _precedent_telemetry_metrics(conn, window_days),
            "fetch_follow": _fetch_follow_metrics(conn, window_days),
            "citation_event_log": _citation_event_log_metrics(conn, window_days),
            "guard_block": _guard_block_metrics(conn, window_days),
        }
    finally:
        conn.close()


def load_packages(packages_file: str | None) -> list[dict] | None:
    """--packages-file を読み込みパースする。未指定時は None を返す。

    ファイルは go-package 機械可読ブロックの JSON 配列でなければならない。
    """
    if packages_file is None:
        return None
    text = Path(packages_file).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("--packages-file must contain a JSON array of go-package blocks")
    if not all(isinstance(item, dict) for item in data):
        raise ValueError("--packages-file の各要素は go-package オブジェクト(JSON object)でなければなりません")
    return data


def _format_rate(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1%}"


def format_text(metrics: dict) -> str:
    """人間向けテキストレポートを組み立てる。"""
    window_days = metrics["window_days"]
    window_label = "全期間" if window_days is None else f"直近{window_days}日"
    lines = [f"=== ops_metrics ({window_label}) ==="]

    c = metrics["contradiction"]
    lines.append(
        f"矛盾イベント数: {c['count']} 件"
        f" (existing_correct={c['by_resolution']['existing_correct']}"
        f", new_correct={c['by_resolution']['new_correct']}"
        f", unresolved={c['by_resolution']['unresolved']}"
        f", unknown={c['by_resolution']['unknown']})"
    )

    _WRITER_NOTE = "（書き手コードなし、report_signalの手動報告のみ。0件/低値は構造的な可能性がある）"

    r = metrics["rollback"]
    lines.append(
        f"巻き戻し率: {_format_rate(r['rate'])}"
        f" ({r['rollback_count']}/{r['post_veto_live_count']})"
        + (_WRITER_NOTE if r.get("no_writer_code") else "")
    )

    s = metrics["shadow_divergence"]
    lines.append(
        f"shadow乖離率: {_format_rate(s['divergence_rate'])}"
        f" ({s['diverged_count']}/{s['shadow_total']})"
        f"  うちfalse_negative: {_format_rate(s['false_negative_rate'])}"
        f" ({s['false_negative_count']}/{s['shadow_total']})"
        + (_WRITER_NOTE if s.get("no_writer_code") else "")
    )

    p = metrics["pull"]
    if "miss_rate" in p:
        lines.append(
            f"pull miss率: {_format_rate(p['miss_rate'])}"
            f" ({p['miss_count']}/{p['citation_slot_count']})"
            + (_WRITER_NOTE if p.get("no_writer_code") else "")
        )
    else:
        lines.append(
            f"pull miss件数: {p['miss_count']} 件（--packages-file 未供給のため率は算出不可）"
            + (_WRITER_NOTE if p.get("no_writer_code") else "")
        )

    m = metrics["precedent_misapplied"]
    if "misapplied_rate" in m:
        lines.append(
            f"誤類推率: {_format_rate(m['misapplied_rate'])}"
            f" ({m['misapplied_count']}/{m['applied_citation_count']})"
            + (_WRITER_NOTE if m.get("no_writer_code") else "")
        )
    else:
        lines.append(
            f"誤類推件数: {m['misapplied_count']} 件（--packages-file 未供給のため率は算出不可）"
            + (_WRITER_NOTE if m.get("no_writer_code") else "")
        )

    g = metrics["goal"]
    if g is not None:
        lines.append(
            f"goal差し戻し回数: {g['rollback_count']} 件 / goal判定件数: {g['judged_count']} 件"
            f" / goal誤判定率: {_format_rate(g['misjudgment_rate'])}"
            f" / goal放置件数: {g['neglected_count']} 件"
        )

    st = metrics["search_telemetry"]
    lines.append(
        f"search縮退率: {_format_rate(st['degraded_rate'])}"
        f" ({st['degraded_count']}/{st['diagnostics_count']})"
        f" / クエリ拡張発火率: {_format_rate(st['qe_fire_rate'])}"
        f" ({st['qe_fired_count']}/{st['diagnostics_count']})"
        f"（全{st['total_count']}件中diagnostics記録あり{st['diagnostics_count']}件）"
    )

    pt = metrics["precedent_telemetry"]
    lines.append(
        f"precedent_telemetry: {pt['count']} 件"
        f" (enumerated={pt['by_guarantee']['enumerated']}"
        f", routing_miss={pt['by_guarantee']['routing_miss']}"
        f", routing_unavailable={pt['by_guarantee']['routing_unavailable']}"
        f", unknown={pt['by_guarantee']['unknown']})"
        f" / enumerated時の列挙カバレッジ: {_format_rate(pt['enumerated_full_coverage_rate'])}"
    )

    ff = metrics["fetch_follow"]
    lines.append(
        f"追随率: {_format_rate(ff['follow_rate'])}"
        f" ({ff['followed_count']}/{ff['search_result_count']})"
    )

    ce = metrics["citation_event_log"]
    lines.append(
        f"citation_event_log: {ce['count']} 件"
        f" (exists={ce['by_verification_result']['exists']}"
        f", dangling={ce['by_verification_result']['dangling']}"
        f", skip={ce['by_verification_result']['skip']}"
        f", not_verified={ce['by_verification_result']['not_verified']})"
    )

    gb = metrics["guard_block"]
    lines.append(f"guard_block件数: {gb['total_count']} 件 ({len(gb['by_rule'])} 規則)")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "signal_events + go-package抽出データの突合集計"
            "（巻き戻し率・shadow乖離率・矛盾/miss/誤類推件数・goal観測・"
            "search/precedent/fetch/citation telemetryの集計・guard_block件数）"
        )
    )
    parser.add_argument(
        "--window-days", type=int, default=30,
        help="集計対象期間（日数、デフォルト30）。0以下を指定すると全期間を対象にする",
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="DBファイルパス（省略時は src.db.get_db_path() の解決先を使う）",
    )
    parser.add_argument("--json", action="store_true", help="JSON形式で出力する")
    parser.add_argument(
        "--packages-file", type=str, default=None,
        help="go_package.py extract/shadow-report が出力するgo-package機械可読ブロックのJSON配列ファイル",
    )
    args = parser.parse_args(argv)

    db_path = args.db
    if db_path is None:
        from src.db import get_db_path

        db_path = get_db_path()

    window_days = args.window_days if args.window_days and args.window_days > 0 else None

    try:
        packages = load_packages(args.packages_file)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"--packages-file の読み込みに失敗しました: {e}", file=sys.stderr)
        return 1

    # load_packages はトップレベルが dict の配列であることまでしか検証しない。
    # precedents/pull など入れ子のフォーマット逸脱は集計中に AttributeError/
    # TypeError として現れるため、素の traceback を出さず制御されたエラーに変換する。
    try:
        metrics = compute_metrics(db_path, window_days=window_days, packages=packages)
    except (AttributeError, TypeError) as e:
        print(f"--packages-file のデータ形式が不正です: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(format_text(metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
