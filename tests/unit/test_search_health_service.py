"""search_health_service.check_search_health() のテスト

検証項目:
1. サンプルなし（search_telemetryが空）は健全
2. 縮退率が閾値以上かつサンプル数が最小値以上で異常判定（degraded_unhealthy）
3. 縮退率が閾値未満なら異常判定にならない
4. クエリ拡張の発火率が閾値（既定0.0）以下かつサンプル数が最小値以上で異常判定（qe_unhealthy）
5. サンプル数が最小値未満のときは、比率が100%異常でも判定しない（誤検知防止の下限）
6. window_days より古い行は集計対象から除外される
7. max_sample を超える行数があるとき、timestamp降順で新しい側だけが対象になる
8. degraded/qe_expansionsキーが欠けた行はサンプルから除外される
9. degraded=Trueの行はクエリ拡張側の母集団から除外される（embedding停止との二重検知を避ける）
"""
import json
from datetime import UTC, datetime, timedelta

from src import config
from src.db import get_connection
from src.services.search_health_service import check_search_health

_DEFAULTS = dict(
    window_days=7,
    max_sample=100,
    min_sample=20,
    degraded_threshold=0.2,
    qe_fire_floor=0.0,
)


def _seed_row(conn, *, degraded: bool | None, qe_expansions: list | None, days_ago: float = 0.0,
              missing_keys: bool = False) -> None:
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S.%f")
    if missing_keys:
        diagnostics = {}
    else:
        diagnostics = {"degraded": degraded, "qe_expansions": qe_expansions}
    conn.execute(
        "INSERT INTO search_telemetry (query, parameters, result_count, diagnostics_json, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (json.dumps("q"), json.dumps({}), 0, json.dumps(diagnostics), ts),
    )


def _seed_many(conn, count: int, *, degraded: bool, qe_expansions: list, start_days_ago: float,
               step_seconds: float = 1.0) -> None:
    """count件の行を、start_days_agoを起点にstep_seconds間隔でtimestampをずらして挿入する。

    （新しい順にSELECTされる前提のテストでtie-breakを避けるため、timestampを必ず分散させる）
    """
    for i in range(count):
        days_ago = start_days_ago - (i * step_seconds) / 86400
        _seed_row(conn, degraded=degraded, qe_expansions=qe_expansions, days_ago=days_ago)


def test_no_rows_is_healthy(temp_db):
    conn = get_connection()
    try:
        result = check_search_health(conn, **_DEFAULTS)
    finally:
        conn.close()

    assert result.is_healthy is True
    assert result.warnings == []
    assert result.degraded_ratio is None
    assert result.qe_fired_ratio is None


def test_omitted_args_fall_back_to_config_defaults(temp_db, monkeypatch):
    """全引数省略時はsrc.configの値を呼び出し時点で読んで判定される"""
    monkeypatch.setattr(config, "SEARCH_HEALTH_MIN_SAMPLE", 10)
    conn = get_connection()
    try:
        _seed_many(conn, 10, degraded=True, qe_expansions=["x"], start_days_ago=0.1)
        conn.commit()
        result = check_search_health(conn)
    finally:
        conn.close()

    # min_sampleを10に差し替えたので10件で評価対象になり、縮退率が出る
    assert result.degraded_sample_count == 10
    assert result.degraded_ratio == 1.0


def test_missing_telemetry_table_is_healthy(temp_db):
    """search_telemetryが無い環境は計測不能なだけで、異常として扱わない"""
    conn = get_connection()
    try:
        conn.execute("DROP TABLE search_telemetry")
        conn.commit()
        result = check_search_health(conn)
    finally:
        conn.close()

    assert result.is_healthy is True
    assert result.degraded_sample_count == 0


def test_degraded_ratio_above_threshold_triggers(temp_db):
    conn = get_connection()
    try:
        # 20件中5件degraded = 25% >= 20%閾値。QE側はdegraded行以外は全部発火ありにして無関係に保つ
        _seed_many(conn, 5, degraded=True, qe_expansions=["x"], start_days_ago=0.1)
        _seed_many(conn, 15, degraded=False, qe_expansions=["x"], start_days_ago=0.2)
        conn.commit()
        result = check_search_health(conn, **_DEFAULTS)
    finally:
        conn.close()

    assert result.degraded_sample_count == 20
    assert result.degraded_count == 5
    assert result.degraded_ratio == 0.25
    assert result.degraded_unhealthy is True
    assert result.qe_unhealthy is False
    assert result.is_healthy is False
    assert len(result.warnings) == 1


def test_degraded_ratio_below_threshold_does_not_trigger(temp_db):
    conn = get_connection()
    try:
        # 20件中3件degraded = 15% < 20%閾値
        _seed_many(conn, 3, degraded=True, qe_expansions=["x"], start_days_ago=0.1)
        _seed_many(conn, 17, degraded=False, qe_expansions=["x"], start_days_ago=0.2)
        conn.commit()
        result = check_search_health(conn, **_DEFAULTS)
    finally:
        conn.close()

    assert result.degraded_ratio == 0.15
    assert result.degraded_unhealthy is False
    assert result.is_healthy is True


def test_qe_fire_floor_triggers_when_never_fires(temp_db):
    conn = get_connection()
    try:
        # 25件すべてqe_expansions空（発火率0%）、degradedは全部False（無関係）
        _seed_many(conn, 25, degraded=False, qe_expansions=[], start_days_ago=0.1)
        conn.commit()
        result = check_search_health(conn, **_DEFAULTS)
    finally:
        conn.close()

    assert result.qe_sample_count == 25
    assert result.qe_fired_count == 0
    assert result.qe_fired_ratio == 0.0
    assert result.qe_unhealthy is True
    assert result.degraded_unhealthy is False
    assert result.is_healthy is False
    assert len(result.warnings) == 1


def test_qe_fires_at_least_once_does_not_trigger(temp_db):
    conn = get_connection()
    try:
        _seed_many(conn, 1, degraded=False, qe_expansions=["tag"], start_days_ago=0.1)
        _seed_many(conn, 24, degraded=False, qe_expansions=[], start_days_ago=0.2)
        conn.commit()
        result = check_search_health(conn, **_DEFAULTS)
    finally:
        conn.close()

    assert result.qe_fired_count == 1
    assert result.qe_fired_ratio > 0.0
    assert result.qe_unhealthy is False
    assert result.is_healthy is True


def test_sample_below_min_does_not_trigger_even_at_100_percent(temp_db):
    conn = get_connection()
    try:
        # 19件（min_sample=20未満）が全部degraded・QE不発火でも判定しない
        _seed_many(conn, 19, degraded=True, qe_expansions=[], start_days_ago=0.1)
        conn.commit()
        result = check_search_health(conn, **_DEFAULTS)
    finally:
        conn.close()

    assert result.degraded_sample_count == 19
    assert result.degraded_ratio is None
    assert result.qe_fired_ratio is None
    assert result.degraded_unhealthy is False
    assert result.qe_unhealthy is False
    assert result.is_healthy is True


def test_rows_outside_window_are_excluded(temp_db):
    conn = get_connection()
    try:
        # window_days=7より古い30件（全degraded）はサンプル対象外。
        # window内の5件（min_sample=20未満）だけが残るため判定は発火しない
        _seed_many(conn, 30, degraded=True, qe_expansions=[], start_days_ago=10.0)
        _seed_many(conn, 5, degraded=False, qe_expansions=["x"], start_days_ago=0.1)
        conn.commit()
        result = check_search_health(conn, **_DEFAULTS)
    finally:
        conn.close()

    assert result.degraded_sample_count == 5
    assert result.degraded_unhealthy is False
    assert result.is_healthy is True


def test_max_sample_limits_to_most_recent_rows(temp_db):
    conn = get_connection()
    try:
        # 古い50件はdegraded、新しい100件はdegraded=Falseにして timestamp を分散させる。
        # max_sample=100 なら新しい100件だけが対象になり縮退率0%で健全のはず
        _seed_many(conn, 50, degraded=True, qe_expansions=["x"], start_days_ago=1.0, step_seconds=1.0)
        _seed_many(conn, 100, degraded=False, qe_expansions=["x"], start_days_ago=0.1, step_seconds=1.0)
        conn.commit()
        result = check_search_health(conn, **_DEFAULTS)
    finally:
        conn.close()

    assert result.degraded_sample_count == 100
    assert result.degraded_count == 0
    assert result.degraded_unhealthy is False
    assert result.is_healthy is True


def test_degraded_rows_are_excluded_from_qe_sample(temp_db):
    """embedding停止による縮退(degraded=True)は、QE側の母集団に数えない。

    クエリ拡張もembedding_service経由でtag_vecを検索するため、embedding停止中は
    qe_expansionsが構造的に空になる。これをQEの母集団に含めると、embedding停止の
    1件の障害が「縮退」と「クエリ拡張停止」の2つの異常として二重に検知されてしまう。
    """
    conn = get_connection()
    try:
        # 25件全部degraded（embedding停止中はqe_expansionsも常に空になる）
        _seed_many(conn, 25, degraded=True, qe_expansions=[], start_days_ago=0.1)
        conn.commit()
        result = check_search_health(conn, **_DEFAULTS)
    finally:
        conn.close()

    assert result.degraded_sample_count == 25
    assert result.degraded_unhealthy is True
    # QE側はdegraded行しかないため母集団ゼロ＝評価不能（異常としては検知しない）
    assert result.qe_sample_count == 0
    assert result.qe_fired_ratio is None
    assert result.qe_unhealthy is False


def test_rows_missing_expected_keys_are_excluded_from_sample(temp_db):
    conn = get_connection()
    try:
        _seed_row(conn, degraded=None, qe_expansions=None, days_ago=0.1, missing_keys=True)
        _seed_many(conn, 20, degraded=False, qe_expansions=["x"], start_days_ago=0.2)
        conn.commit()
        result = check_search_health(conn, **_DEFAULTS)
    finally:
        conn.close()

    # missing_keys行は除外され、残り20件のみがサンプルになる
    assert result.degraded_sample_count == 20
    assert result.qe_sample_count == 20
