"""Bounded CDP calls to the application's loopback-only Chrome instance."""
from __future__ import annotations

import json
import re
import threading
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, HTTPRedirectHandler, build_opener


class ChromeControlError(RuntimeError):
    def __init__(self, code="browser_unavailable"):
        self.code = code
        super().__init__(code)


def loopback_url(value: str, *, websocket=False, port=None) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError()
        if any(ord(char) < 33 for char in value) or (port is not None and (type(port) is not int or not 1 <= port <= 65535)):
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme != ("ws" if websocket else "http") or parsed.hostname != "127.0.0.1"
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or not parsed.port or port is not None and parsed.port != port
                or "\\" in value or not re.fullmatch(r"/[A-Za-z0-9/_-]+", parsed.path)):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ChromeControlError() from None


class CdpConnection:
    def __init__(self, url: str, *, port: int, connector=None):
        url = loopback_url(url, websocket=True, port=port)
        if connector is None:
            import websocket
            connector = websocket.create_connection
        # Do not send this local control endpoint through any configured proxy.
        try:
            self.socket = connector(url, timeout=3, suppress_origin=True, http_no_proxy=["127.0.0.1"])
        except Exception:
            raise ChromeControlError() from None
        self._lock, self._serial = threading.Lock(), 0

    def call(self, method: str, params=None, *, timeout=20):
        try:
            return self._call(method, params, timeout=timeout)
        except ChromeControlError:
            raise
        except Exception:
            raise ChromeControlError("script_unavailable") from None

    def _call(self, method: str, params=None, *, timeout=20):
        with self._lock:
            self._serial += 1
            token = self._serial
            self.socket.send(json.dumps({"id": token, "method": method, "params": params or {}}, ensure_ascii=True))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self.socket.settimeout(max(0.05, deadline - time.monotonic()))
                raw = self.socket.recv()
                if not isinstance(raw, str) or len(raw) > 1024 * 1024:
                    raise ChromeControlError("invalid_response")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ChromeControlError("invalid_response")
                if value.get("id") != token:
                    continue
                if "error" in value:
                    raise ChromeControlError("script_unavailable")
                result = value.get("result", {})
                if not isinstance(result, dict):
                    raise ChromeControlError("invalid_response")
                return result
            raise ChromeControlError("script_timeout")

    def close(self):
        try:
            self.socket.close()
        except Exception:
            pass


def local_json(port: int, route: str):
    url = loopback_url(f"http://127.0.0.1:{port}{route}", port=port)
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ChromeControlError()
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=3) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ChromeControlError("invalid_response")
        value = json.loads(raw)
        if not isinstance(value, (dict, list)):
            raise ChromeControlError("invalid_response")
        return value
    except ChromeControlError:
        raise
    except Exception:
        raise ChromeControlError("invalid_response") from None
