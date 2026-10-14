"""The system `UrlOpener` against a real local listener, and `FakeUrlOpener`'s own contract."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from base.host.net.ports import SYSTEM_URL_OPENER
from base.host.net.tests.fake_url_opener import Answer, FakeUrlOpener


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status = 503 if self.path == "/down" else 200
        body = b'{"ok": true}' if status == 200 else b'{"ok": false}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture
def listener_url() -> Iterator[str]:
    """A real HTTP listener on an ephemeral port of the loopback interface."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_system_opener_reads_a_url_and_a_request(listener_url: str) -> None:
    with SYSTEM_URL_OPENER(f"{listener_url}/health", 5.0) as response:
        assert (response.status, response.read()) == (200, b'{"ok": true}')
    request = urllib.request.Request(f"{listener_url}/health")  # noqa: S310 — the loopback listener of this test
    with SYSTEM_URL_OPENER(request, 5.0) as response:
        assert response.headers["Content-Type"] == "application/json"


def test_system_opener_raises_http_error_outside_2xx(listener_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as caught:
        SYSTEM_URL_OPENER(f"{listener_url}/down", 5.0)
    assert (caught.value.code, caught.value.read()) == (503, b'{"ok": false}')


def test_fake_answers_by_prefix_and_records_each_request() -> None:
    opener = FakeUrlOpener()
    opener.on("http://a.test/health", Answer(b'{"ok": true}', headers={"Content-Length": "12"}))
    request = urllib.request.Request("http://a.test/health?x=1", data=b"{}", method="POST")
    request.add_header("X-Token", "t")
    with opener(request, 2.5) as response:
        assert (response.status, response.headers["Content-Length"]) == (200, "12")
        assert response.read(2) == b'{"'
        assert response.read() == b'ok": true}'
    [call] = opener.calls
    assert (call.url, call.timeout, call.method, call.data) == (
        "http://a.test/health?x=1",
        2.5,
        "POST",
        b"{}",
    )
    assert call.headers == {"X-token": "t"}


def test_fake_raises_http_error_for_a_non_2xx_answer_like_urlopen() -> None:
    opener = FakeUrlOpener()
    opener.on("http://a.test", Answer("down", status=503, headers={"Retry-After": "3"}))
    with pytest.raises(urllib.error.HTTPError) as caught:
        opener("http://a.test/health", 1.0)
    assert (caught.value.code, caught.value.read(), caught.value.headers["Retry-After"]) == (
        503,
        b"down",
        "3",
    )
    assert opener.urls == ["http://a.test/health"]


def test_fake_plays_answers_in_order_and_raises_scripted_exceptions() -> None:
    opener = FakeUrlOpener()
    opener.on("http://a.test", urllib.error.URLError("refused"), Answer(b"up"))
    with pytest.raises(urllib.error.URLError):
        opener("http://a.test/x", 1.0)
    with opener("http://a.test/x", 1.0) as response:
        assert response.read() == b"up"
    with pytest.raises(AssertionError, match="ran out"):
        opener("http://a.test/x", 1.0)


def test_fake_repeat_serves_the_last_answer_and_the_first_matching_route_wins() -> None:
    opener = FakeUrlOpener()
    opener.on("http://a.test/specific", Answer(b"specific"))
    opener.on("http://a.test", Answer(b"general"), repeat=True)
    for _ in range(2):
        with opener("http://a.test/other", 1.0) as response:
            assert response.read() == b"general"
    with opener("http://a.test/specific", 1.0) as response:
        assert response.read() == b"specific"


def test_fake_refuses_an_unscripted_url_and_an_empty_route() -> None:
    opener = FakeUrlOpener()
    opener.on("http://a.test", Answer())
    with pytest.raises(AssertionError, match=r"unscripted request: http://b\.test/"):
        opener("http://b.test/", 1.0)
    with pytest.raises(ValueError, match="at least one answer"):
        opener.on("http://c.test")


def test_fake_predicate_route_matches_the_whole_url() -> None:
    opener = FakeUrlOpener()
    opener.on(lambda url: url.endswith("/metrics"), Answer(b"m"), repeat=True)
    with opener("http://any.test:9/metrics", 1.0) as response:
        assert response.read() == b"m"
