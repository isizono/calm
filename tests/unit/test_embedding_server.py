"""src/infra/embedding_server.py のユニットテスト

allow_reuse_addressはクラス属性としてモジュール読み込み時にsys.platformから
決まるため、importlib.reloadで実際に再評価させて両platformの値を検証する
（reloadの副作用は実platformでの値に戻してテスト終了後に残さない）。
"""
import importlib
import sys as _real_sys
import types

import pytest

from src.infra import embedding_server

_ACTUAL_PLATFORM = _real_sys.platform


def _reload_under_platform(monkeypatch, platform_name):
    monkeypatch.setattr(embedding_server.sys, "platform", platform_name)
    importlib.reload(embedding_server)


class TestHost:
    def test_host_is_ipv4_loopback(self):
        """localhostではなく127.0.0.1を使う（::1優先環境での接続遅延を避けるため）"""
        assert embedding_server.HOST == "127.0.0.1"


class TestAllowReuseAddress:
    def test_enabled_on_non_windows(self, monkeypatch):
        try:
            _reload_under_platform(monkeypatch, "darwin")
            assert embedding_server.EmbeddingHTTPServer.allow_reuse_address is True
        finally:
            _reload_under_platform(monkeypatch, _ACTUAL_PLATFORM)

    def test_disabled_on_windows(self, monkeypatch):
        """WindowsのSO_REUSEADDRは使用中ポートへのbindを許し多重起動防止が効かなく
        なるため、Windowsでは無効化する"""
        try:
            _reload_under_platform(monkeypatch, "win32")
            assert embedding_server.EmbeddingHTTPServer.allow_reuse_address is False
        finally:
            # importlib.reloadの副作用(クラス属性の固定)を実platformに戻す
            _reload_under_platform(monkeypatch, _ACTUAL_PLATFORM)


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
    monkeypatch.setitem(_real_sys.modules, "sentence_transformers", module)
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
