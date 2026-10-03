"""src/infra/embedding_server.py のユニットテスト

allow_reuse_addressはクラス属性としてモジュール読み込み時にsys.platformから
決まるため、importlib.reloadで実際に再評価させて両platformの値を検証する
（reloadの副作用は実platformでの値に戻してテスト終了後に残さない）。
"""
import importlib
import sys as _real_sys

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
