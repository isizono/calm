"""ow_send()のユニットテスト（リトライ区分）

エッジケース:
- 4xx レスポンス → 即座にエラー返却（リトライしない）
- 5xx/接続断 → 指数バックオフ後にリトライ → 最終的にエラー
- 成功時 → {"msg_id": int}を返す
"""
import json
import urllib.error
import urllib.request

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


class TestOwSendTerminatedIsPlainSend:
    """event:state(terminated) の送信は通常のrelay送信として完結する。

    terminated(cause=closed|cancelled) を含むstate eventを送信しても、
    relay POST /send 以外の副作用（プロセス操作等）は一切発生しない。
    """

    @pytest.fixture
    def capture_relay(self, monkeypatch):
        """relay POST 成功 (msg_id 返却) を固定で返し、呼び出しURLを記録する stub。"""
        calls = []

        class FakeResponse:
            def read(self):
                return json.dumps({"msg_id": 123}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")
        return calls

    @pytest.fixture
    def forbid_subprocess(self, monkeypatch):
        """ow_send がプロセス操作系の副作用を持たないことを保証するガード。"""

        def _fail(*args, **kwargs):
            raise AssertionError("ow_send must not spawn or run subprocesses")

        monkeypatch.setattr(ow_service.subprocess, "run", _fail)
        monkeypatch.setattr(ow_service.subprocess, "Popen", _fail)

    @pytest.mark.parametrize("cause", ["closed", "cancelled"])
    def test_terminated_close_event_returns_msg_id_without_side_effect(
        self, capture_relay, forbid_subprocess, cause
    ):
        """terminated(cause=closed|cancelled) → relay送信1回のみで成功する"""
        body = {"v": 1, "kind": "event", "from": "w-a", "to": "*",
                "data": {"type": "state", "state": "terminated", "cause": cause}}
        result = ow_service.ow_send(channel="AbCdEfGh", handle="w-a", body=body)

        assert result == {"msg_id": 123}
        # relay POST /send 1回のみ。追加のHTTP呼び出しは発生しない
        assert capture_relay == ["http://127.0.0.1:8765/send"]

    @pytest.mark.parametrize("cause", ["dead", "crashed"])
    def test_terminated_other_causes_also_plain_send(
        self, capture_relay, forbid_subprocess, cause
    ):
        """terminated(cause=dead|crashed) も同様に通常送信として成功する"""
        body = {"v": 1, "kind": "event", "from": "w-a", "to": "*",
                "data": {"type": "state", "state": "terminated", "cause": cause}}
        result = ow_service.ow_send(channel="AbCdEfGh", handle="w-a", body=body)

        assert result == {"msg_id": 123}
        assert capture_relay == ["http://127.0.0.1:8765/send"]


class TestV1MessagingStandalone:
    """ow_send / ow_history はrelay HTTPのみに依存して単独で機能する。"""

    @pytest.fixture
    def forbid_subprocess(self, monkeypatch):
        def _fail(*args, **kwargs):
            raise AssertionError("v1 messaging must not touch subprocesses")

        monkeypatch.setattr(ow_service.subprocess, "run", _fail)
        monkeypatch.setattr(ow_service.subprocess, "Popen", _fail)

    def test_ow_send_completes_with_relay_http_only(self, monkeypatch, forbid_subprocess):
        """ow_send はrelayへのPOSTだけで完結する"""
        calls = []

        class FakeResponse:
            def read(self):
                return json.dumps({"msg_id": 7}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")

        body = {"v": 1, "kind": "command", "from": "orch", "to": "w-a", "verb": "ping"}
        result = ow_service.ow_send(channel="AbCdEfGh", handle="orch", body=body)

        assert result == {"msg_id": 7}
        assert calls == ["http://127.0.0.1:8765/send"]

    def test_ow_history_completes_with_relay_http_only(self, monkeypatch, forbid_subprocess):
        """ow_history はrelayへのGETだけで完結し、body JSONをパースして返す"""
        calls = []

        class FakeResponse:
            def read(self):
                return json.dumps({"messages": [
                    {"msg_id": 5, "handle": "w-a", "body": '{"v":1}'},
                ]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")

        result = ow_service.ow_history(channel="AbCdEfGh", since=0, limit=10)

        assert len(calls) == 1
        assert "/history?" in calls[0]
        assert result["messages"][0]["body"] == {"v": 1}
