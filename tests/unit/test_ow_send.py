"""ow_send()のユニットテスト（リトライ区分）

エッジケース:
- 4xx レスポンス → 即座にエラー返却（リトライしない）
- 5xx/接続断 → 指数バックオフ後にリトライ → 最終的にエラー
- 成功時 → {"msg_id": int}を返す
"""
import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.services import ow_service


def _make_http_error(code: int, message: str = "error") -> urllib.error.HTTPError:
    """urllib.error.HTTPErrorを作成するヘルパー"""
    return urllib.error.HTTPError(
        url="http://127.0.0.1:8765/send",
        code=code,
        msg=message,
        hdrs={},
        fp=None,
    )


class TestOwSend4xx:
    """4xxレスポンスは即座に失敗（リトライなし）"""

    def test_404_returns_error_immediately(self, monkeypatch):
        """404 Not Found → リトライなしで即エラー"""
        call_count = 0

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            raise _make_http_error(404, "channel not found")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")

        result = ow_service._relay_request("POST", "/send", {"channel": "xxx", "handle": "orch", "body": "{}"})

        assert "error" in result
        assert result["error"]["code"] == 404
        # 4xxはリトライしないので1回のみ呼ばれる
        assert call_count == 1

    def test_400_returns_error_immediately(self, monkeypatch):
        """400 Bad Request → リトライなしで即エラー"""
        call_count = 0

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            raise _make_http_error(400, "missing required fields")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")

        result = ow_service._relay_request("POST", "/send", {"channel": "xxx", "handle": "orch", "body": "{}"})

        assert "error" in result
        assert result["error"]["code"] == 400
        assert call_count == 1

    def test_403_returns_error_immediately(self, monkeypatch):
        """403 Forbidden → リトライなしで即エラー"""
        call_count = 0

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            raise _make_http_error(403, "forbidden")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")

        result = ow_service._relay_request("POST", "/send", {})
        assert "error" in result
        assert call_count == 1


class TestOwSend5xx:
    """5xx/接続断は指数バックオフでリトライ"""

    def test_500_retries_and_raises(self, monkeypatch):
        """500 Server Error → MAX_RETRIES回リトライ後にraiseされる"""
        call_count = 0
        max_retries = ow_service._MAX_RETRIES

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            raise _make_http_error(500, "internal server error")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")
        # sleepを無効化してテストを高速化
        monkeypatch.setattr(ow_service.time, "sleep", lambda _: None)

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            ow_service._relay_request("POST", "/send", {})

        assert exc_info.value.code == 500
        # MAX_RETRIES+1回（初回 + リトライ回数）呼ばれる
        assert call_count == max_retries + 1

    def test_connection_error_retries_and_raises(self, monkeypatch):
        """接続断（URLError）→ MAX_RETRIES回リトライ後にraiseされる"""
        call_count = 0
        max_retries = ow_service._MAX_RETRIES

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")
        monkeypatch.setattr(ow_service.time, "sleep", lambda _: None)

        with pytest.raises(urllib.error.URLError):
            ow_service._relay_request("POST", "/send", {})

        assert call_count == max_retries + 1

    def test_5xx_eventually_succeeds_after_retry(self, monkeypatch):
        """最初に5xxでも、2回目で成功すれば結果を返す"""
        call_count = 0

        class FakeResponse:
            def __init__(self):
                self._data = json.dumps({"msg_id": 42}).encode()

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _make_http_error(503, "service unavailable")
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")
        monkeypatch.setattr(ow_service.time, "sleep", lambda _: None)

        result = ow_service._relay_request("POST", "/send", {"channel": "ch", "handle": "orch", "body": "{}"})

        assert result.get("msg_id") == 42
        assert call_count == 2


class TestOwSendSuccess:
    """正常系"""

    def test_success_returns_msg_id(self, monkeypatch):
        """正常時は {"msg_id": int} を返す"""

        class FakeResponse:
            def read(self):
                return json.dumps({"msg_id": 10}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: FakeResponse())
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")

        result = ow_service.ow_send(
            channel="AbCdEfGh",
            handle="orch",
            body={"v": 1, "kind": "cmd", "to": "w-a", "verb": "ping"},
        )
        assert result.get("msg_id") == 10

    def test_ow_history_since_passed_in_url(self, monkeypatch):
        """ow_history: since値がURLクエリパラメータとして正しく渡される（ケース#3）"""
        captured_urls = []

        class FakeResponse:
            def read(self):
                return json.dumps({"messages": [
                    {"msg_id": 5, "handle": "w-a", "body": '{"v":1}'},
                    {"msg_id": 6, "handle": "w-a", "body": '{"v":1}'},
                ]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")

        result = ow_service.ow_history(channel="AbCd", since=4, limit=50)

        assert "since=4" in captured_urls[0]
        assert "limit=50" in captured_urls[0]
        assert len(result["messages"]) == 2
        assert result["messages"][0]["msg_id"] == 5

    def test_body_serialized_as_json_string(self, monkeypatch):
        """bodyはJSON文字列としてrelayに送信される"""
        sent_data = {}

        class FakeResponse:
            def read(self):
                return json.dumps({"msg_id": 1}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None):
            sent_data.update(json.loads(req.data))
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")

        ow_body = {"v": 1, "kind": "cmd", "to": "w-a", "verb": "ping"}
        ow_service.ow_send(channel="ch", handle="orch", body=ow_body)

        # bodyはJSON文字列として格納されている
        assert isinstance(sent_data["body"], str)
        parsed_body = json.loads(sent_data["body"])
        assert parsed_body == ow_body



class TestEnsureChannel:
    """ensure_channel: channel未存在時の自動作成"""

    def test_ensure_channel_success(self, monkeypatch):
        """POST /createが成功すればTrueを返す"""

        class FakeResponse:
            def read(self):
                return json.dumps({"channel_code": "TestCh01"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: FakeResponse())
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")

        result = ow_service.ensure_channel("TestCh01")
        assert result is True

    def test_ensure_channel_failure_returns_false(self, monkeypatch):
        """POST /createが5xxで失敗してもFalseを返す（例外は外に出ない）"""

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                url="http://127.0.0.1:8765/create",
                code=500,
                msg="internal server error",
                hdrs={},
                fp=None,
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")
        monkeypatch.setattr(ow_service.time, "sleep", lambda _: None)

        result = ow_service.ensure_channel("TestCh01")
        assert result is False


class TestOwSendEnsureChannel:
    """ow_send: channel未存在時のensure_channel自動作成フロー"""

    def test_404_triggers_ensure_channel_and_resend(self, monkeypatch):
        """ow_sendでchannel 404 → ensure_channelが呼ばれ再送が成功する"""
        call_log = []

        class FakeCreateResponse:
            def read(self):
                return json.dumps({"channel_code": "AbCdEfGh"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class FakeSendResponse:
            def read(self):
                return json.dumps({"msg_id": 99}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            call_log.append(url)
            if "/send" in url and len([u for u in call_log if "/send" in u]) == 1:
                # 1回目のsendは404
                raise urllib.error.HTTPError(
                    url=url, code=404, msg="channel not found", hdrs={}, fp=None
                )
            elif "/create" in url:
                return FakeCreateResponse()
            else:
                # 2回目のsend（再送）は成功
                return FakeSendResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")

        result = ow_service.ow_send(
            channel="AbCdEfGh",
            handle="orch",
            body={"v": 1, "kind": "cmd", "to": "w-a", "verb": "ping"},
        )

        assert result.get("msg_id") == 99
        # /createと2回目の/sendが呼ばれている
        assert any("/create" in u for u in call_log)
        assert len([u for u in call_log if "/send" in u]) == 2

    def test_404_ensure_channel_fails_returns_error(self, monkeypatch):
        """ow_sendでchannel 404 → ensure_channel失敗 → 元の404エラーを返す"""

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                url=req.full_url, code=404, msg="not found", hdrs={}, fp=None
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")

        result = ow_service.ow_send(
            channel="NoExist1",
            handle="orch",
            body={"v": 1, "kind": "state", "state": "ready"},
        )

        assert "error" in result
        assert result["error"]["code"] == 404


class TestOwStatusEnsure:
    """ow_status: relay起動 + channel作成の自動化"""

    def test_ow_status_calls_ensure_relay_and_channel(self, monkeypatch, tmp_path):
        """ow_statusはensure_relay_serverとensure_channelを自動実行する"""
        ensure_relay_called = []
        ensure_channel_called = []

        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: ensure_relay_called.append(True) or True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: ensure_channel_called.append(ch) or True)
        monkeypatch.setattr(ow_service, "_relay_request", lambda *args, **kwargs: {"handles": []})
        monkeypatch.setattr(ow_service, "find_topic_id_by_channel", lambda *_args, **_kw: None)

        ow_service.ow_status(channel="TestCh01", topic_id="454")

        assert len(ensure_relay_called) == 1
        assert ensure_channel_called == ["TestCh01"]

    def test_ow_status_relay_unavailable_returns_error(self, monkeypatch):
        """relayが起動できない場合はエラーを返す"""
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: False)

        result = ow_service.ow_status(channel="TestCh01")

        assert "error" in result
        assert result["error"]["code"] == "RELAY_UNAVAILABLE"
