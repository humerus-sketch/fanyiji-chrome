"""Loopback CDP limits with fake sockets and HTTP openers; no network calls."""
from collections import deque
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request

import pytest

from fanyiji import chrome_cdp as module
from fanyiji.chrome_cdp import CdpConnection, ChromeControlError, local_json, loopback_url


PORT = 9222
HTTP_URL = "http://127.0.0.1:9222/json/list"
WS_URL = "ws://127.0.0.1:9222/devtools/page/owned-test"
LIMIT = 1024 * 1024


class FakeSocket:
    def __init__(self, responses=()):
        self.responses = deque(responses)
        self.sent, self.timeouts = [], []
        self.closed = False

    def send(self, value):
        self.sent.append(json.loads(value))

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self):
        value = self.responses.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, value):
        self.value = value
        self.read_sizes = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def read(self, limit):
        self.read_sizes.append(limit)
        return self.value[:limit]


@pytest.mark.parametrize("url,websocket", [(HTTP_URL, False), (WS_URL, True)])
def test_exact_loopback_endpoint_is_allowed(url, websocket):
    assert loopback_url(url, websocket=websocket, port=PORT) == url


@pytest.mark.parametrize("url,websocket", [
    ("https://127.0.0.1:9222/json/list", False), ("wss://127.0.0.1:9222/devtools/page/owned-test", True),
    ("http://localhost:9222/json/list", False), ("http://[::1]:9222/json/list", False),
    ("http://127.0.0.1.example:9222/json/list", False), ("http://8.8.8.8:9222/json/list", False),
    ("http://user:private_value@127.0.0.1:9222/json/list", False),
    ("http://@127.0.0.1:9222/json/list", False), ("ws://@127.0.0.1:9222/devtools/page/owned-test", True),
    ("http://127.0.0.1:9222/json/list?token=private_value", False),
    ("http://127.0.0.1:9222/json/list#private_value", False),
    ("http://127.0.0.1:9223/json/list", False), ("http://127.0.0.1:0/json/list", False),
    ("http://127.0.0.1:65536/json/list", False), ("http://127.0.0.1/json/list", False),
    ("http://127.0.0.1:9222/../json/list", False), ("http://127.0.0.1:9222/json\\list", False),
    ("http://127.0.0.1:9222/中文", False), ("\nhttp://127.0.0.1:9222/json/list", False),
    ("http://127.0.0.1:9222/json/\nlist", False),
])
def test_foreign_credentialed_or_malformed_endpoint_is_rejected_without_echo(url, websocket):
    with pytest.raises(ChromeControlError) as error:
        loopback_url(url, websocket=websocket, port=PORT)
    assert "private_value" not in str(error.value)


@pytest.mark.parametrize("value", [None, True, 9222, [], {}, b"http://127.0.0.1:9222/json/list"])
def test_non_text_endpoint_is_rejected(value):
    with pytest.raises(ChromeControlError):
        loopback_url(value, port=PORT)


def test_invalid_socket_endpoint_never_invokes_connector():
    connector = Mock()
    with pytest.raises(ChromeControlError):
        CdpConnection("ws://other-host:9222/devtools/page/foreign", port=PORT, connector=connector)
    connector.assert_not_called()


def test_socket_connection_uses_loopback_no_proxy_and_bounded_timeout(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://not-used.invalid:8888")
    monkeypatch.setenv("HTTPS_PROXY", "http://not-used.invalid:8888")
    socket = FakeSocket([json.dumps({"id": 1, "result": {"ok": True}})])
    connector = Mock(return_value=socket)
    connection = CdpConnection(WS_URL, port=PORT, connector=connector)
    options = connector.call_args.kwargs
    assert connector.call_args.args == (WS_URL,)
    assert options["timeout"] == 3 and options["suppress_origin"] is True
    assert "127.0.0.1" in options["http_no_proxy"]
    assert connection.call("Runtime.evaluate", {"expression": "fixed_test"}, timeout=2) == {"ok": True}
    assert socket.sent == [{"id": 1, "method": "Runtime.evaluate", "params": {"expression": "fixed_test"}}]
    assert all(0 < value <= 2 for value in socket.timeouts)
    connection.close()
    assert socket.closed


def test_events_and_wrong_reply_ids_cannot_be_used_as_the_request_result():
    socket = FakeSocket([json.dumps({"method": "Target.changed", "params": {"value": "ignore"}}),
                         json.dumps({"id": 99, "result": {"wrong": True}}),
                         json.dumps({"id": 1, "result": {"right": True}})])
    connection = CdpConnection(WS_URL, port=PORT, connector=lambda *_args, **_kwargs: socket)
    assert connection.call("Page.reload") == {"right": True}
    assert len(socket.sent) == 1 and socket.sent[0]["params"] == {}


@pytest.mark.parametrize("response", ["x" * (LIMIT + 1), b"binary", "[1,2]", "not-json", "null"],
                         ids=["oversized", "binary", "json-array", "invalid-json", "json-null"])
def test_malformed_or_oversized_socket_reply_fails_with_sanitized_error(response):
    socket = FakeSocket([response])
    connection = CdpConnection(WS_URL, port=PORT, connector=lambda *_args, **_kwargs: socket)
    with pytest.raises(ChromeControlError):
        connection.call("Page.reload")


def test_cdp_error_drops_remote_message_and_details():
    socket = FakeSocket([json.dumps({"id": 1, "error": {"code": -1, "message": "private_value", "data": "secret_data"}})])
    connection = CdpConnection(WS_URL, port=PORT, connector=lambda *_args, **_kwargs: socket)
    with pytest.raises(ChromeControlError) as error:
        connection.call("Page.reload")
    assert error.value.code == "script_unavailable"
    assert "private_value" not in str(error.value) and "secret_data" not in str(error.value)


def test_transport_timeout_is_sanitized_and_not_retried():
    socket = FakeSocket([TimeoutError("private_value")])
    connection = CdpConnection(WS_URL, port=PORT, connector=lambda *_args, **_kwargs: socket)
    with pytest.raises(ChromeControlError) as error:
        connection.call("Runtime.evaluate", timeout=1)
    assert "private_value" not in str(error.value)
    assert len(socket.sent) == 1


def test_local_http_opener_disables_proxy_and_caps_the_body(monkeypatch):
    response = FakeResponse(b'[{"type":"page"}]')
    opener = SimpleNamespace(open=Mock(return_value=response))
    builder = Mock(return_value=opener)
    monkeypatch.setattr(module, "build_opener", builder)
    assert local_json(PORT, "/json/list") == [{"type": "page"}]
    assert any(isinstance(handler, ProxyHandler) and handler.proxies == {} for handler in builder.call_args.args)
    opener.open.assert_called_once_with(HTTP_URL, timeout=3)
    assert response.read_sizes == [LIMIT + 1] and response.closed


def test_local_http_rejects_redirects_instead_of_following_a_foreign_endpoint(monkeypatch):
    response = FakeResponse(b"[]")
    builder = Mock(return_value=SimpleNamespace(open=Mock(return_value=response)))
    monkeypatch.setattr(module, "build_opener", builder)
    local_json(PORT, "/json/list")
    redirect = next((handler for handler in builder.call_args.args if isinstance(handler, HTTPRedirectHandler)), None)
    assert redirect is not None, "The default HTTP redirect handler can leave the loopback endpoint."
    try:
        redirected = redirect.redirect_request(Request(HTTP_URL), None, 302, "Found", {}, "http://foreign.invalid/secret")
    except ChromeControlError:
        return
    assert redirected is None


@pytest.mark.parametrize("body", [b"x" * (LIMIT + 1), b"private_value is not json"], ids=["oversized", "invalid-json"])
def test_local_http_invalid_response_is_bounded_and_not_exposed(monkeypatch, body):
    response = FakeResponse(body)
    monkeypatch.setattr(module, "build_opener", lambda *_args: SimpleNamespace(open=Mock(return_value=response)))
    with pytest.raises(ChromeControlError) as error:
        local_json(PORT, "/json/list")
    assert "private_value" not in str(error.value)
    assert response.read_sizes == [LIMIT + 1]
