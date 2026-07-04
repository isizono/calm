"""scripts/ops_metrics.py の単体テスト

signal_events テーブルに record_signal で fixture 行を積み、定義式どおりの
巻き戻し率・shadow乖離率・矛盾/miss/誤類推件数が計算されること、分母0での
N/A 表示、--packages-file 供給/未供給でのフォールバックを検証する。
"""
import json
import sqlite3

import pytest

from scripts.ops_metrics import (
    compute_metrics,
    format_text,
    load_packages,
    main,
)
from src.services import signal_service as ss


def _backdate(db_path: str, signal_id: int, days_ago: int) -> None:
    """指定シグナルの first_seen_at / last_seen_at を days_ago 日前に書き換える。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE signal_events
            SET first_seen_at = datetime('now', ?), last_seen_at = datetime('now', ?)
            WHERE id = ?
            """,
            (f"-{days_ago} days", f"-{days_ago} days", signal_id),
        )
        conn.commit()
    finally:
        conn.close()


class TestContradictionMetrics:
    def test_counts_by_resolution(self, temp_db):
        ss.record_signal("contradiction", "a vs b", source="agent",
                          context={"resolution": "existing_correct"})
        ss.record_signal("contradiction", "c vs d", source="agent",
                          context={"resolution": "new_correct"})
        ss.record_signal("contradiction", "e vs f", source="agent",
                          context={"resolution": "unresolved"})
        ss.record_signal("contradiction", "g vs h", source="agent", context=None)

        metrics = compute_metrics(temp_db, window_days=None)

        c = metrics["contradiction"]
        assert c["count"] == 4
        assert c["by_resolution"] == {
            "existing_correct": 1,
            "new_correct": 1,
            "unresolved": 1,
            "unknown": 1,
        }


class TestRollbackMetrics:
    def test_rate_computed_from_definition(self, temp_db):
        # post_veto_candidate(live) の分母イベント2件、うちrollback1件
        ss.record_signal(
            "boundary_case", "case PR#1", source="gate",
            context={"mode": "live", "machine_verdict": "post_veto_candidate"},
        )
        ss.record_signal(
            "boundary_case", "case PR#2", source="gate",
            context={"mode": "live", "machine_verdict": "post_veto_candidate"},
        )
        # 対象外(mode=shadow)は分母に入らない
        ss.record_signal(
            "boundary_case", "case PR#3", source="gate",
            context={"mode": "shadow", "machine_verdict": "post_veto_candidate"},
        )
        # 対象外(machine_verdict=pre_go)は分母に入らない
        ss.record_signal(
            "boundary_case", "case PR#4", source="gate",
            context={"mode": "live", "machine_verdict": "pre_go"},
        )
        ss.record_signal("rollback", "revert PR#1", source="gate",
                          context={"target": {"pr": 1}, "reason": "bug"})

        metrics = compute_metrics(temp_db, window_days=None)

        r = metrics["rollback"]
        assert r["rollback_count"] == 1
        assert r["post_veto_live_count"] == 2
        assert r["rate"] == pytest.approx(0.5)

    def test_rate_is_none_when_denominator_zero(self, temp_db):
        metrics = compute_metrics(temp_db, window_days=None)

        r = metrics["rollback"]
        assert r["rollback_count"] == 0
        assert r["post_veto_live_count"] == 0
        assert r["rate"] is None
        assert "N/A" in format_text(metrics)


class TestShadowDivergenceMetrics:
    def test_divergence_and_false_negative_rate(self, temp_db):
        ss.record_signal("boundary_case", "shadow case 1", source="gate",
                          context={"mode": "shadow", "divergence": "none"})
        ss.record_signal("boundary_case", "shadow case 2", source="gate",
                          context={"mode": "shadow", "divergence": "false_negative"})
        ss.record_signal("boundary_case", "shadow case 3", source="gate",
                          context={"mode": "shadow", "divergence": "false_positive"})
        ss.record_signal("boundary_case", "shadow case 4", source="gate",
                          context={"mode": "shadow", "divergence": "gray_case"})
        # live は shadow の分母に入らない
        ss.record_signal("boundary_case", "live case", source="gate",
                          context={"mode": "live", "machine_verdict": "pre_go"})

        metrics = compute_metrics(temp_db, window_days=None)

        s = metrics["shadow_divergence"]
        assert s["shadow_total"] == 4
        assert s["diverged_count"] == 3
        assert s["divergence_rate"] == pytest.approx(0.75)
        assert s["false_negative_count"] == 1
        assert s["false_negative_rate"] == pytest.approx(0.25)

    def test_rate_is_none_when_no_shadow_cases(self, temp_db):
        metrics = compute_metrics(temp_db, window_days=None)

        s = metrics["shadow_divergence"]
        assert s["shadow_total"] == 0
        assert s["divergence_rate"] is None
        assert s["false_negative_rate"] is None


class TestPullAndMisappliedMetrics:
    _PACKAGES = [
        {
            "precedents": [
                {"ref": "decision 1", "stance": "applied"},
                {"ref": "decision 2", "stance": "out_of_scope"},
            ],
            "pull": {"presented": [{"type": "decision", "id": 1}, {"type": "decision", "id": 3}]},
        },
        {
            "precedents": [{"ref": "decision 4", "stance": "applied"}],
            "pull": {"presented": "unavailable"},
        },
    ]

    def test_without_packages_file_returns_count_only(self, temp_db):
        ss.record_signal("precedent_miss", "missed decision 9", source="agent",
                          context={"missed_ids": [{"type": "decision", "id": 9}]})
        ss.record_signal("precedent_misapplied", "decision 1 out of scope", source="agent",
                          context={"cited_id": {"type": "decision", "id": 1}})

        metrics = compute_metrics(temp_db, window_days=None, packages=None)

        assert metrics["pull"] == {"miss_count": 1}
        assert metrics["precedent_misapplied"] == {"misapplied_count": 1}
        text = format_text(metrics)
        assert "率は算出不可" in text

    def test_with_packages_file_computes_rates(self, temp_db):
        ss.record_signal("precedent_miss", "missed decision 9", source="agent")
        ss.record_signal("precedent_misapplied", "decision 1 out of scope", source="agent")

        metrics = compute_metrics(temp_db, window_days=None, packages=self._PACKAGES)

        # citation_slot_count = precedents(2+1) + presented(2, unavailableは非対象) = 5
        assert metrics["pull"]["citation_slot_count"] == 5
        assert metrics["pull"]["miss_count"] == 1
        assert metrics["pull"]["miss_rate"] == pytest.approx(1 / 5)

        # applied_citation_count = stance=applied の2件
        assert metrics["precedent_misapplied"]["applied_citation_count"] == 2
        assert metrics["precedent_misapplied"]["misapplied_count"] == 1
        assert metrics["precedent_misapplied"]["misapplied_rate"] == pytest.approx(0.5)


class TestWindowDaysFiltering:
    def test_rows_outside_window_are_excluded(self, temp_db):
        recent = ss.record_signal("rollback", "revert PR#recent", source="gate")
        old = ss.record_signal("rollback", "revert PR#old", source="gate")
        _backdate(temp_db, old["id"], days_ago=90)

        metrics = compute_metrics(temp_db, window_days=30)

        assert metrics["rollback"]["rollback_count"] == 1

        metrics_all = compute_metrics(temp_db, window_days=None)
        assert metrics_all["rollback"]["rollback_count"] == 2


class TestLoadPackages:
    def test_load_packages_reads_json_array(self, tmp_path):
        packages_file = tmp_path / "packages.json"
        packages_file.write_text(json.dumps(self.__class__._sample()), encoding="utf-8")

        loaded = load_packages(str(packages_file))

        assert loaded == self.__class__._sample()

    def test_load_packages_none_when_not_given(self):
        assert load_packages(None) is None

    def test_load_packages_rejects_non_array(self, tmp_path):
        packages_file = tmp_path / "packages.json"
        packages_file.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

        with pytest.raises(ValueError):
            load_packages(str(packages_file))

    def test_load_packages_rejects_non_dict_element(self, tmp_path):
        packages_file = tmp_path / "packages.json"
        packages_file.write_text(json.dumps([{"precedents": []}, 42]), encoding="utf-8")

        with pytest.raises(ValueError):
            load_packages(str(packages_file))

    @staticmethod
    def _sample():
        return [{"precedents": [], "pull": {"presented": "unavailable"}}]


class TestMainCli:
    def test_main_json_output(self, temp_db, capsys):
        ss.record_signal("contradiction", "a vs b", source="agent",
                          context={"resolution": "unresolved"})

        exit_code = main(["--db", temp_db, "--json", "--window-days", "0"])

        assert exit_code == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["window_days"] is None
        assert payload["contradiction"]["count"] == 1

    def test_main_text_output(self, temp_db, capsys):
        exit_code = main(["--db", temp_db])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "ops_metrics" in out
        assert "矛盾イベント数" in out

    def test_main_with_packages_file(self, temp_db, tmp_path, capsys):
        packages_file = tmp_path / "packages.json"
        packages_file.write_text(
            json.dumps([{"precedents": [{"ref": "decision 1", "stance": "applied"}],
                         "pull": {"presented": []}}]),
            encoding="utf-8",
        )
        ss.record_signal("precedent_misapplied", "decision 1 out of scope", source="agent")

        exit_code = main(["--db", temp_db, "--json", "--packages-file", str(packages_file)])

        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["precedent_misapplied"]["misapplied_rate"] == pytest.approx(1.0)

    def test_main_rejects_broken_packages_file(self, temp_db, tmp_path, capsys):
        packages_file = tmp_path / "broken.json"
        packages_file.write_text("not json", encoding="utf-8")

        exit_code = main(["--db", temp_db, "--packages-file", str(packages_file)])

        assert exit_code == 1
        assert "読み込みに失敗" in capsys.readouterr().err

    def test_main_rejects_non_dict_package_element(self, temp_db, tmp_path, capsys):
        """トップレベル要素が JSON object でない --packages-file を load 時に弾く。"""
        packages_file = tmp_path / "bad.json"
        packages_file.write_text(json.dumps(["not an object"]), encoding="utf-8")

        exit_code = main(["--db", temp_db, "--packages-file", str(packages_file)])

        assert exit_code == 1
        assert "読み込みに失敗" in capsys.readouterr().err

    def test_main_converts_malformed_precedents_to_controlled_error(self, temp_db, tmp_path, capsys):
        """precedents に dict でない要素が混じる --packages-file を素の traceback にせず終了コード1へ変換する。"""
        packages_file = tmp_path / "bad.json"
        packages_file.write_text(
            json.dumps([{"precedents": ["not a dict"], "pull": {"presented": []}}]),
            encoding="utf-8",
        )

        exit_code = main(["--db", temp_db, "--packages-file", str(packages_file)])

        assert exit_code == 1
        assert "データ形式が不正" in capsys.readouterr().err
