"""embedding_serverのモデル読み込みのテスト"""
import sys
import types

import pytest

from src.infra import embedding_server


@pytest.fixture
def fake_sentence_transformer(monkeypatch):
    """SentenceTransformerの呼び出しを記録する偽モジュールを差し込む。

    cachedがFalseの間は、local_files_only=Trueの呼び出しで
    ローカルキャッシュ不在時と同じOSErrorを送出する。
    """
    calls = []
    state = {"cached": True}

    def _fake(name, **kwargs):
        calls.append(kwargs)
        if kwargs.get("local_files_only") and not state["cached"]:
            raise OSError("couldn't find them in the cached files")
        return object()

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _fake
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(embedding_server, "_model", None)
    return calls, state


def test_load_model_uses_local_cache_without_hub_access(fake_sentence_transformer):
    """キャッシュ済みならlocal_files_only=Trueの1回だけで読み込む"""
    calls, _ = fake_sentence_transformer

    embedding_server._load_model()

    assert calls == [{"device": "cpu", "local_files_only": True}]
    assert embedding_server._model is not None


def test_load_model_falls_back_to_hub_when_not_cached(fake_sentence_transformer):
    """キャッシュに無ければHubから取得し直す"""
    calls, state = fake_sentence_transformer
    state["cached"] = False

    embedding_server._load_model()

    assert calls == [
        {"device": "cpu", "local_files_only": True},
        {"device": "cpu"},
    ]
    assert embedding_server._model is not None
