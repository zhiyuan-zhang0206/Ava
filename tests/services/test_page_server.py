"""HTTP-level tests for the static page server."""

from __future__ import annotations

import functools
import http.client
import threading
from pathlib import Path
from types import TracebackType

from services.page_server import server as page_server

_HOST = "127.0.0.1"
_TOKEN = "page-server-test-token"  # noqa: S105 - test-only liveness value


class _RunningPageServer:
    """Run the production page handler against one temporary directory."""

    def __init__(self, directory: Path) -> None:
        self._previous_token = page_server._PageHandler.token
        page_server._PageHandler.token = _TOKEN
        handler = functools.partial(page_server._PageHandler, directory=str(directory))
        self._server = page_server._ReuseTCPServer((_HOST, 0), handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _RunningPageServer:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        page_server._PageHandler.token = self._previous_token


def _get(port: int, path: str) -> tuple[int, str | None, bytes]:
    connection = http.client.HTTPConnection(_HOST, port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()
    finally:
        connection.close()


def test_directory_without_index_returns_placeholder(tmp_path: Path) -> None:
    """An index-less root denies listing without exposing its file names."""
    (tmp_path / "secret.txt").write_text("private", encoding="utf-8")

    with _RunningPageServer(tmp_path) as server:
        status, content_type, body = _get(server.port, "/")

    assert status == 403
    assert content_type == "text/html; charset=utf-8"
    assert b"Directory listings are disabled for ava.ui.serve pages." in body
    assert b"ava.ui.serve_markdown()" in body
    assert b"index.html" in body
    assert b"secret.txt" not in body


def test_directory_with_index_serves_index(tmp_path: Path) -> None:
    """An index file remains the directory response."""
    index = b"<!doctype html><h1>Page content</h1>"
    (tmp_path / "index.html").write_bytes(index)

    with _RunningPageServer(tmp_path) as server:
        status, content_type, body = _get(server.port, "/")

    assert status == 200
    assert content_type == "text/html"
    assert body == index


def test_health_returns_launch_token(tmp_path: Path) -> None:
    """The liveness endpoint keeps its token-bearing response."""
    with _RunningPageServer(tmp_path) as server:
        status, content_type, body = _get(server.port, "/health")

    assert status == 200
    assert content_type == "text/plain"
    assert body == f"ok:{_TOKEN}".encode()


def test_direct_file_path_remains_available(tmp_path: Path) -> None:
    """Banning listings does not ban direct file requests."""
    content = b"hello from a direct file"
    (tmp_path / "hello.txt").write_bytes(content)

    with _RunningPageServer(tmp_path) as server:
        status, _content_type, body = _get(server.port, "/hello.txt")

    assert status == 200
    assert body == content


def test_subdirectory_without_index_returns_placeholder(tmp_path: Path) -> None:
    """An index-less subdirectory denies listing without exposing names."""
    subdirectory = tmp_path / "sub"
    subdirectory.mkdir()
    (subdirectory / "nested-secret.txt").write_text("private", encoding="utf-8")

    with _RunningPageServer(tmp_path) as server:
        status, content_type, body = _get(server.port, "/sub/")

    assert status == 403
    assert content_type == "text/html; charset=utf-8"
    assert b"Directory listings are disabled for ava.ui.serve pages." in body
    assert b"nested-secret.txt" not in body
