"""scripts/check_mypy_baseline.py のラチェット判定を検証する unit test。

mypy の実行（subprocess）は外部境界なので、run_mypy の差し替えと
subprocess.run のモックで扱う。判定・集計・基準値ファイルの読み書きは実物で通す。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import scripts.check_mypy_baseline as cmb  # noqa: E402


def _line(file: str, line: int, severity: str = "error", code: str | None = "arg-type") -> str:
    return json.dumps(
        {
            "file": file,
            "line": line,
            "column": 1,
            "message": f"msg at {line}",
            "hint": None,
            "code": code,
            "severity": severity,
        }
    )


def _run(tmp_path: Path, monkeypatch, output: str, baseline: dict | None, argv=None):
    baseline_path = tmp_path / "mypy-baseline.json"
    if baseline is not None:
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(cmb, "run_mypy", lambda repo_root: output)
    code = cmb.main(argv or [], repo_root=tmp_path, baseline_path=baseline_path)
    return code, baseline_path


class TestParseMypyJson:
    def test_counts_only_errors_and_skips_non_json_lines(self):
        output = "\n".join(
            [
                _line("src/a.py", 1),
                _line("src/a.py", 2, severity="note", code=None),
                "Found 1 error in 1 file (checked 3 source files)",
                _line("src\\b.py", 5, code=None),
            ]
        )
        errors = cmb.parse_mypy_json(output)
        assert [(e.file, e.line, e.code) for e in errors] == [
            ("src/a.py", 1, "arg-type"),
            ("src/b.py", 5, None),
        ]


class TestCompare:
    def test_reports_increase_decrease_new_and_fixed_files_per_file(self):
        baseline = {"a.py": 3, "b.py": 2, "gone.py": 1}
        current = {"a.py": 2, "b.py": 2, "new.py": 1}
        result = cmb.compare(baseline, current)
        assert result.increased == {"new.py": (0, 1)}
        assert result.decreased == {"a.py": (3, 2), "gone.py": (1, 0)}

    def test_increase_in_one_file_is_not_hidden_by_decrease_in_another(self):
        result = cmb.compare({"a.py": 5, "b.py": 1}, {"a.py": 3, "b.py": 2})
        assert result.increased == {"b.py": (1, 2)}


class TestMain:
    def test_fails_and_shows_errors_of_the_increased_file(self, tmp_path, monkeypatch, capsys):
        output = "\n".join([_line("src/a.py", 1), _line("src/a.py", 9), _line("src/b.py", 4)])
        code, _ = _run(tmp_path, monkeypatch, output, {"src/a.py": 1, "src/b.py": 1})
        assert code == 1
        err = capsys.readouterr().err
        assert "src/a.py: 1 -> 2" in err
        assert "src/a.py:9: error: msg at 9  [arg-type]" in err
        assert "src/b.py:4" not in err

    def test_passes_with_guidance_when_errors_decreased(self, tmp_path, monkeypatch, capsys):
        code, baseline_path = _run(tmp_path, monkeypatch, _line("src/a.py", 1), {"src/a.py": 3})
        assert code == 0
        out = capsys.readouterr().out
        assert "src/a.py: 3 -> 1" in out
        assert "--update" in out
        # 判定だけでは基準値を書き換えない
        assert json.loads(baseline_path.read_text(encoding="utf-8")) == {"src/a.py": 3}

    def test_missing_baseline_treats_every_error_as_new(self, tmp_path, monkeypatch):
        code, _ = _run(tmp_path, monkeypatch, _line("src/a.py", 1), None)
        assert code == 1

    def test_update_writes_current_counts_sorted(self, tmp_path, monkeypatch):
        output = "\n".join([_line("src/z.py", 1), _line("src/a.py", 1), _line("src/a.py", 2)])
        code, baseline_path = _run(
            tmp_path, monkeypatch, output, {"src/a.py": 5, "src/fixed.py": 2}, argv=["--update"]
        )
        assert code == 0
        text = baseline_path.read_text(encoding="utf-8")
        assert json.loads(text) == {"src/a.py": 2, "src/z.py": 1}
        assert list(json.loads(text)) == ["src/a.py", "src/z.py"]

    def test_mypy_crash_fails_with_its_message(self, tmp_path, monkeypatch, capsys):
        def boom(repo_root):
            raise cmb.MypyRunError("mypy が終了コード 2 で失敗しました:\nbad config")

        monkeypatch.setattr(cmb, "run_mypy", boom)
        code = cmb.main([], repo_root=tmp_path, baseline_path=tmp_path / "b.json")
        assert code == 2
        assert "bad config" in capsys.readouterr().err


class TestRunMypy:
    @pytest.mark.parametrize("returncode", [0, 1])
    def test_type_errors_are_not_a_run_failure(self, monkeypatch, tmp_path, returncode):
        def fake_run(cmd, **kwargs):
            assert cmd[1:] == ["-m", "mypy", "--output", "json"]
            return subprocess.CompletedProcess(cmd, returncode, stdout="OUT", stderr="")

        monkeypatch.setattr(cmb.subprocess, "run", fake_run)
        assert cmb.run_mypy(tmp_path) == "OUT"

    def test_other_exit_codes_raise(self, monkeypatch, tmp_path):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="error: bad config")

        monkeypatch.setattr(cmb.subprocess, "run", fake_run)
        with pytest.raises(cmb.MypyRunError, match="bad config"):
            cmb.run_mypy(tmp_path)
