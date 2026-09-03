"""Standalone page HTTP server — doorplate ③ (R3).

One process per open `agent_pages` row whose serve_dir is set, spawned by
the page_server daemon. Serves the row's directory over HTTP on the row's
port, bound to the machine's reachable host (loopback on a single box —
the gateway reverse-proxies it, so nothing needs it on the network).

`/health` answers `ok:<token>` with the per-launch token the daemon
generated, so the daemon's liveness poll can prove the server on the port
is the one it just spawned — a stale occupant from an earlier launch can
never satisfy it (the reclaim logic that used to live in ava.ui now lives
here, in the supervisor).

Usage (spawned by the daemon, never by hand):
    .venv/bin/python -m services.page_server.server --port P --host H --dir D

The per-launch token arrives in `PAGE_SERVER_TOKEN` (env, not argv — audit
round-2 security P2-4: secrets never ride argv, `ps` would show them).
"""

from __future__ import annotations

import argparse
import http.server
import io
import os
import socketserver
from contextlib import suppress
from http import HTTPStatus


class _PageHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the directory with a /health liveness endpoint carrying the
    per-launch token, plus the content-type map the UI pages rely on."""

    token = ""

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(("ok:" + self.token).encode())
            return None
        return super().do_GET()

    def list_directory(self, path: str | os.PathLike[str]) -> io.BytesIO:
        """Return a placeholder instead of exposing directory contents."""
        del path
        body = b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Directory unavailable</title>
</head>
<body>
  <main>
    <h1>This directory is not browsable</h1>
    <p>Directory listings are disabled for ava.ui.serve pages.</p>
    <p>Create an <code>index.html</code> in the served directory. For Markdown,
      render it to a self-contained HTML page first with the ava-ui markdown widget.</p>
  </main>
</body>
</html>
"""
        self.send_response(HTTPStatus.FORBIDDEN)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def handle_one_request(self) -> None:
        try:
            return super().handle_one_request()
        except (TimeoutError, ConnectionResetError, BrokenPipeError):
            pass  # fail-fast-ok: client hung up mid-request; nothing to do


_EXTENSIONS = {
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".woff2": "font/woff2",
    ".wasm": "application/wasm",
}


class _ReuseTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _configure_handler(token: str) -> None:
    _PageHandler.token = token
    _PageHandler.extensions_map.update(_EXTENSIONS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ava page server (R3 doorplate 3)")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--dir", required=True)
    args = parser.parse_args()

    # Token via env (see module docstring): argv stays clean.
    token = os.environ.get("PAGE_SERVER_TOKEN", "")
    if not token:
        parser.error("PAGE_SERVER_TOKEN not set in environment")
    _configure_handler(token)
    # SimpleHTTPRequestHandler serves the process cwd unless `directory` is
    # given; the daemon passes --dir, so chdir to it before serving (otherwise
    # every page serves the daemon's cwd as a directory listing).
    os.chdir(args.dir)
    with (
        _ReuseTCPServer((args.host, args.port), _PageHandler) as server,
        suppress(KeyboardInterrupt),
    ):
        server.serve_forever()


if __name__ == "__main__":
    main()
