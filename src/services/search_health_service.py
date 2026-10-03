"""search_telemetry の直近記録から検索品質の劣化を検知する。

ベクトル検索が利用不可でキーワード検索のみへ縮退した状態、クエリ拡張が
発火しない状態は、どちらもsearch()の戻り値には現れず、後からsearch_telemetry
を手で集計しないと気づけない。本モジュールはSessionStart起動時にその集計を
代わりに行い、閾値超過時のみ結果を返す。

DBの行数急減検知（backup_service.health_check/should_take_snapshot）と同じ
運用（呼び出し元が閾値超過時のみ注意を表示する、通常時は空扱い）に揃える。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class SearchHealthResult:
    """check_search_health() の結果。

    degraded_ratio/qe_fired_ratio は対応するサンプル数がmin_sample未満のとき
    None（評価不能、異常判定もFalse固定）になる。qe_sample_count/qe_fired_countは
    degraded=Trueの行を含まない（理由はcheck_search_health内のコメント参照）。
    """
    is_healthy: bool = True
    warnings: list[str] = field(default_factory=list)
    degraded_sample_count: int = 0
    degraded_count: int = 0
    degraded_ratio: float | None = None
    degraded_unhealthy: bool = False
    qe_sample_count: int = 0
    qe_fired_count: int = 0
    qe_fired_ratio: float | None = None
    qe_unhealthy: bool = False


def _usable_diagnostics(raw: str | None) -> dict | None:
    """diagnostics_json列の1行をparseし、degraded/qe_expansionsキーが揃っているものだけ返す。

    両キーが揃わない行（旧スキーマ・想定外の壊れたJSON）は判定材料に混ぜず
    サンプルから除外する（どちらの比率も歪めない安全側の判断）。
    """
    if not raw:
        return None
    try:
        diag = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(diag, dict):
        return None
    if "degraded" not in diag or "qe_expansions" not in diag:
        return None
    return diag


def check_search_health(
    conn: sqlite3.Connection,
    *,
    window_days: int | None = None,
    max_sample: int | None = None,
    min_sample: int | None = None,
    degraded_threshold: float | None = None,
    qe_fire_floor: float | None = None,
) -> SearchHealthResult:
    """直近search_telemetryのdiagnostics_jsonから縮退率・クエリ拡張発火率を集計する。

    window_days以内かつ最大max_sample件（timestamp降順）を対象にする。
    各比率はサンプル数がmin_sample未満のときは評価せず常に健全扱いにする
    （誤検知を避けるための安全側の下限）。省略した引数はsrc.configの既定値を使う。
    """
    from src import config

    window_days = config.SEARCH_HEALTH_WINDOW_DAYS if window_days is None else window_days
    max_sample = config.SEARCH_HEALTH_MAX_SAMPLE if max_sample is None else max_sample
    min_sample = config.SEARCH_HEALTH_MIN_SAMPLE if min_sample is None else min_sample
    degraded_threshold = (
        config.SEARCH_HEALTH_DEGRADED_RATIO if degraded_threshold is None else degraded_threshold
    )
    qe_fire_floor = (
        config.SEARCH_HEALTH_QE_FIRE_FLOOR if qe_fire_floor is None else qe_fire_floor
    )

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        rows = conn.execute(
            "SELECT diagnostics_json FROM search_telemetry "
            "WHERE timestamp >= ? AND diagnostics_json IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT ?",
            (cutoff, max_sample),
        ).fetchall()
    except sqlite3.OperationalError:
        # search_telemetryテーブル自体が無い等。計測不能なだけで異常ではない。
        return SearchHealthResult()

    degraded_sample = 0
    degraded_count = 0
    qe_sample = 0
    qe_fired_count = 0
    for row in rows:
        diag = _usable_diagnostics(row["diagnostics_json"])
        if diag is None:
            continue
        degraded_sample += 1
        if diag["degraded"]:
            degraded_count += 1
            # クエリ拡張（_expand_query_with_tags）もembedding_service経由で
            # tag_vecをKNN検索するため、ベクトル検索自体が利用不可な行は
            # 必然的にqe_expansionsが空になる。QEの母集団に含めると
            # embedding停止がQE側の異常としても二重に数えられ、
            # tag_vec側固有の不具合と区別できなくなるため除外する。
            continue
        qe_sample += 1
        if diag["qe_expansions"]:
            qe_fired_count += 1

    result = SearchHealthResult(
        degraded_sample_count=degraded_sample,
        degraded_count=degraded_count,
        qe_sample_count=qe_sample,
        qe_fired_count=qe_fired_count,
    )

    if degraded_sample >= min_sample:
        result.degraded_ratio = degraded_count / degraded_sample
        if result.degraded_ratio >= degraded_threshold:
            result.degraded_unhealthy = True
            result.warnings.append(
                f"- 検索の縮退率: {result.degraded_ratio:.0%}"
                f"（直近{degraded_sample}件中{degraded_count}件、閾値{degraded_threshold:.0%}）"
            )

    if qe_sample >= min_sample:
        result.qe_fired_ratio = qe_fired_count / qe_sample
        if result.qe_fired_ratio <= qe_fire_floor:
            result.qe_unhealthy = True
            result.warnings.append(
                f"- クエリ拡張の発火率: {result.qe_fired_ratio:.0%}"
                f"（直近{qe_sample}件中{qe_fired_count}件、閾値{qe_fire_floor:.0%}以下）"
            )

    result.is_healthy = not (result.degraded_unhealthy or result.qe_unhealthy)
    return result
