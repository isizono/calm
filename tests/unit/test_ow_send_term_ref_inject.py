"""ow_send の identity event term_ref 自動補完テスト（B案 D#2720）。

`_maybe_inject_term_ref()` の単体テスト:
- cache あり・term_ref 未指定 → 補完される
- cache 無し → 触らない
- term_ref 既設定 → 上書きしない
- kind!=event or data.type!=identity → 触らない
- session_id 無し → 触らない
- 元の body / data dict を破壊しない (copy-on-write)
"""
import json
from pathlib import Path

import pytest

from src.services.ow_service import _maybe_inject_term_ref


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _write_cache(home: Path, session_id: str, term_ref: str) -> None:
    cache_dir = home / ".cc-memory" / "ow" / "term_refs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{session_id}.json").write_text(
        json.dumps({"term_ref": term_ref}), encoding="utf-8"
    )


def _identity_body(**data_extra) -> dict:
    data = {"type": "identity", "session_id": "sess-1"}
    data.update(data_extra)
    return {"v": 1, "kind": "event", "from": "w-a", "to": "*", "data": data}


def test_inject_when_cache_exists_and_term_ref_unset(tmp_home):
    _write_cache(tmp_home, "sess-1", "%5")
    out = _maybe_inject_term_ref(_identity_body())
    assert out["data"]["term_ref"] == "%5"


def test_skip_when_cache_missing(tmp_home):
    out = _maybe_inject_term_ref(_identity_body())
    assert "term_ref" not in out["data"]


def test_skip_when_cache_file_invalid_json(tmp_home):
    cache_dir = tmp_home / ".cc-memory" / "ow" / "term_refs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "sess-1.json").write_text("not-json", encoding="utf-8")
    out = _maybe_inject_term_ref(_identity_body())
    assert "term_ref" not in out["data"]


def test_skip_when_cache_term_ref_empty(tmp_home):
    cache_dir = tmp_home / ".cc-memory" / "ow" / "term_refs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "sess-1.json").write_text(json.dumps({"term_ref": ""}), encoding="utf-8")
    out = _maybe_inject_term_ref(_identity_body())
    assert "term_ref" not in out["data"]


def test_does_not_overwrite_existing_term_ref(tmp_home):
    _write_cache(tmp_home, "sess-1", "%cached")
    out = _maybe_inject_term_ref(_identity_body(term_ref="%existing"))
    assert out["data"]["term_ref"] == "%existing"


def test_skip_when_not_identity_type(tmp_home):
    _write_cache(tmp_home, "sess-1", "%5")
    body = {"v": 1, "kind": "event", "data": {"type": "state", "session_id": "sess-1"}}
    out = _maybe_inject_term_ref(body)
    assert "term_ref" not in out["data"]


def test_skip_when_kind_is_command(tmp_home):
    _write_cache(tmp_home, "sess-1", "%5")
    body = {"v": 1, "kind": "command", "data": {"type": "identity", "session_id": "sess-1"}}
    out = _maybe_inject_term_ref(body)
    assert "term_ref" not in out["data"]


def test_skip_when_session_id_missing(tmp_home):
    _write_cache(tmp_home, "sess-1", "%5")
    body = {"v": 1, "kind": "event", "data": {"type": "identity"}}
    out = _maybe_inject_term_ref(body)
    assert "term_ref" not in out["data"]


def test_skip_when_data_not_dict(tmp_home):
    _write_cache(tmp_home, "sess-1", "%5")
    body = {"v": 1, "kind": "event", "data": "not-dict"}
    # 触らず素通し
    out = _maybe_inject_term_ref(body)
    assert out is body or out == body


def test_does_not_mutate_input_body(tmp_home):
    _write_cache(tmp_home, "sess-1", "%5")
    body = _identity_body()
    original_data = body["data"]
    out = _maybe_inject_term_ref(body)
    # 元の data dict は変更されない
    assert "term_ref" not in original_data
    assert out["data"] is not original_data
    assert out["data"]["term_ref"] == "%5"


def test_inject_passes_through_arbitrary_cached_value(tmp_home):
    """_maybe_inject_term_ref は format validation せず cache の値を素通しする。

    cache に未知形式 (UUID 等) が残っていてもエラーにせず注入する契約
    (validation 責任は reducer / classify_term_ref 側にある)。
    """
    _write_cache(tmp_home, "sess-1", "42C08804-2743-49EA-BEC5-F10B5717039B")
    out = _maybe_inject_term_ref(_identity_body())
    assert out["data"]["term_ref"] == "42C08804-2743-49EA-BEC5-F10B5717039B"
