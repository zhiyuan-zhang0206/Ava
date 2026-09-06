"""The gate — the always-up entry to the fleet UI.

Owns the entry port (the port the user bookmarks; `frontend` in the port
table). Serves the static login page and two maintenance pages — "updating"
and "service unavailable" — and proxies the Next.js app (on `app` = entry+1)
whenever the cluster is healthy. A rollout takes the gateway down (503) and restarts the frontend —
the gate is owned by the platform supervisor OUTSIDE service-session teardown, so the entry never
blacks out: users see the updating page during the rollout and land back on
the app automatically when it finishes.

Auth is the gateway's session cookie (host-only, shared across ports). One
immutable `$AVA_HOME/deploy-state.json` snapshot is read at the start of each
request. A valid active generation owns the response immediately; otherwise
the gate forwards Cookie to `GET /api/auth/check` and serves login/app. Every
transport failure (gateway or app) reuses that same snapshot, so a recovering
service cannot make one request say "unavailable" and the next invent
"updating" from a different condition.

The SPA's own API/SSE traffic goes straight to the gateway (never through
this proxy), so a simple buffering proxy suffices — no streaming needed.

All three static pages use a copy of the app's design tokens, down to reading
the theme the user picked in the app — a rollout swaps one for the other, and
that should not look like landing on a different product. They stay
dependency-free: no build output, no network fetch, everything inlined.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from shared.config import settings
from shared.log import logger
from shared.ui_update_state import UiUpdateSnapshot

_log = logging.getLogger("services.gate")

# Headers forwarded to the app / gateway on proxied requests. The app needs the
# browser Host to derive the same gateway origin its client uses; everything
# else (hop-by-hop headers and cookies beyond the session one) is dropped.
_FORWARD_HEADERS = (
    "accept",
    "accept-language",
    "cookie",
    "content-type",
    "host",
    "x-forwarded-host",
    "x-forwarded-proto",
)

# Browser security headers originate at Next.js. They must cross the public
# gate with the app response or the browser never enforces its CSP and related
# hardening headers.
_FORWARD_RESPONSE_HEADERS = (
    "content-security-policy",
    "referrer-policy",
    "x-content-type-options",
    "x-frame-options",
)

_FORWARDED_HOST_RE = re.compile(r"^[A-Za-z0-9.:-]+$")


def _valid_forwarded_header(name: str, value: str) -> bool:
    if name == "x-forwarded-host":
        return _FORWARDED_HOST_RE.fullmatch(value) is not None
    if name == "x-forwarded-proto":
        return value.lower() in ("http", "https")
    return True


_PROBE_TIMEOUT_S = 3.0

# Auth-probe failure categories (audit #1736). The gate's verdict is
# fail-closed ("down") for every one of them — the category exists so an
# incident can be reconstructed from the events table instead of a wall of
# indistinguishable "down" pages. Each category is one value of
# `gate_auth_probe_failed`'s `category` attribute.
PROBE_FAIL_AUTH = "auth"  # gateway answered 401/403 — the probe itself was rejected
PROBE_FAIL_TIMEOUT = "timeout"  # probe exceeded _PROBE_TIMEOUT_S (connect or read)
PROBE_FAIL_NETWORK = "network"  # transport failure: refused, unreachable, reset, DNS
PROBE_FAIL_APPLICATION = "application"  # gateway answered, but not with a valid auth check


def classify_probe_error(exc: BaseException) -> str:
    """Classify one auth-probe exception for observability (audit #1736).

    Purely diagnostic — the verdict stays fail-closed for every category.
    The gateway's ``/api/auth/check`` route is in its auth-bypass set and
    always answers 200, so a 401/403 means the auth layer in front of it
    rejected the probe; any other HTTP error status means the gateway
    answered but failed; a timeout is the probe budget elapsing; a transport
    error means the gateway was unreachable; anything else (non-JSON body,
    unexpected failure) is an application error.
    """
    if isinstance(exc, urllib.error.HTTPError):  # subclass of URLError — check first
        return PROBE_FAIL_AUTH if exc.code in (401, 403) else PROBE_FAIL_APPLICATION
    if isinstance(exc, TimeoutError):  # socket.timeout is TimeoutError on 3.10+
        return PROBE_FAIL_TIMEOUT
    if isinstance(exc, urllib.error.URLError):
        return PROBE_FAIL_TIMEOUT if isinstance(exc.reason, TimeoutError) else PROBE_FAIL_NETWORK
    if isinstance(exc, OSError):  # RemoteDisconnected / ConnectionReset / BrokenPipe / ...
        return PROBE_FAIL_NETWORK
    return PROBE_FAIL_APPLICATION


class Gate:
    """Request-agnostic gate behavior; the HTTP handler is a thin shell."""

    def __init__(
        self,
        *,
        gateway_base: str,
        app_base: str,
        static_dir: Path,
        state_path: str | None = None,
    ) -> None:
        self.gateway_base = gateway_base.rstrip("/")
        self.app_base = app_base.rstrip("/")
        # Every request is answered with one whole page, so a browser never gets
        # a chance to fetch a stylesheet — the shared look and the theme
        # bootstrap have to travel inside the document. Holding them in their
        # own files and inlining here keeps the palette copied from the app in
        # one place instead of once per page. The markers are CSS/JS comments,
        # so an un-substituted page is still a valid document.
        theme = {
            "/*__THEME_CSS__*/": (static_dir / "theme.css").read_text(),
            "/*__THEME_JS__*/": (static_dir / "theme.js").read_text(),
        }

        def page(name: str) -> str:
            body = (static_dir / name).read_text()
            for marker, asset in theme.items():
                body = body.replace(marker, asset)
            return body

        # The login form posts to the gateway origin; substitute it at load so
        # the static page needs no build step and works on any gateway port.
        self.login_page = page("login.html").replace("__GATEWAY_BASE__", self.gateway_base)
        self.updating_page = page("updating.html")
        self.down_page = page("down.html")
        # A path is injected by tests. In production the default resolves from
        # the same AVA_HOME whose rollout/restart spawn owns the generation.
        self.state_path = state_path

    def deploy_snapshot(self) -> UiUpdateSnapshot:
        """One lock-free, atomic-file snapshot for the whole HTTP request."""
        from shared.ui_update_state import read

        return read(self.state_path)

    def verdict(self, cookie: str, snapshot: UiUpdateSnapshot) -> str:
        """ "up" (authenticated, cluster healthy) | "login" (healthy, no session)
        | "updating" (one valid generation owns the maintenance surface)
        | "down" (no valid active generation and a service is not answering)."""
        # The marker is checked before any dependency. Once a generation owns
        # the maintenance surface, gateway/app flapping cannot change the page.
        if snapshot.status == "updating":
            return "updating"
        if snapshot.status == "invalid":
            return "down"
        req = urllib.request.Request(  # noqa: S310 — operator-declared internal URL, never request input
            f"{self.gateway_base}/api/auth/check",
            headers={"Cookie": cookie} if cookie else {},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(  # noqa: S310 — operator-declared internal URL
                req, timeout=_PROBE_TIMEOUT_S
            ) as resp:
                body = json.loads(resp.read())
                return "up" if body.get("authenticated") else "login"
        except Exception as exc:  # any probe failure means "not serving"; never crash a request
            self._report_probe_failure(exc, latency_ms=round((time.monotonic() - started) * 1000))
            return "down"

    def _report_probe_failure(self, exc: BaseException, *, latency_ms: int) -> None:
        """Structured record of one failed auth probe — the diagnostic half
        of the fail-closed verdict (audit #1736).

        One ``gate_auth_probe_failed`` event per failed probe, carrying the
        classification and the exception shape, so an operator can tell an
        auth rejection, a network outage, a probe timeout, and a gateway
        application failure apart from the events table alone — previously
        every one of them collapsed into an unobservable "down".
        """
        logger.warning(
            "gate auth probe failed ({category}) after {latency_ms}ms: {exception_value!r}",
            event="gate_auth_probe_failed",
            category=classify_probe_error(exc),
            exception_type=type(exc).__name__,
            exception_value=str(exc),
            status=exc.code if isinstance(exc, urllib.error.HTTPError) else None,
            latency_ms=latency_ms,
        )

    def proxy_app(self, handler: BaseHTTPRequestHandler, snapshot: UiUpdateSnapshot) -> None:
        """Fetch the path from the app and stream it back; on transport failure
        project maintenance from the request's persisted snapshot.

        An app that answers with an HTTP error status (404, 500, ...) is
        proxied through as-is: the app is up and its answer is meaningful —
        a browser probing /favicon.ico must see the app's 404 so it drops
        its cached icon, not an updating page that keeps the stale icon
        alive forever. Only a transport failure (unreachable, timeout)
        means the app is down."""
        target = f"{self.app_base}{handler.path}"
        try:
            req = urllib.request.Request(  # noqa: S310 — loopback app, fixed internal URL
                target, method=handler.command
            )
            for name in _FORWARD_HEADERS:
                value = handler.headers.get(name)
                if value and _valid_forwarded_header(name, value):
                    req.add_header(name, value)
            if handler.command in ("POST", "PUT", "PATCH"):
                length = int(handler.headers.get("Content-Length") or 0)
                req.data = handler.rfile.read(length) if length else b""
            try:
                with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:  # noqa: S310
                    status, headers, body = resp.status, resp.headers, resp.read()
            except urllib.error.HTTPError as e:  # app up, answered with an error status
                status, headers, body = e.code, e.headers, e.read()
            handler.send_response(status)
            for name, value in headers.items():
                if name.lower() in (
                    "content-type",
                    "cache-control",
                    "etag",
                    *_FORWARD_RESPONSE_HEADERS,
                ):
                    handler.send_header(name, value)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
        except Exception:  # transport failure — use the SAME request snapshot
            _log.warning("app proxy failed for %s", handler.path)
            self.serve_maintenance(handler, snapshot)

    def serve_maintenance(
        self, handler: BaseHTTPRequestHandler, snapshot: UiUpdateSnapshot
    ) -> None:
        """Exhaustive maintenance projection from one persisted snapshot."""
        if snapshot.status != "updating":
            self._serve_static(handler, self.down_page, 503)
            return
        if snapshot.started_at is None:
            self._serve_static(handler, self.down_page, 503)
            return
        now = dt.datetime.now(dt.UTC)
        # The server and marker live on the same gateway host. Compute the base
        # here, clamp a future timestamp, and let the page add performance.now()
        # so browser wall-clock drift/storage policy cannot reset the duration.
        base_ms = max(0, int((now - snapshot.started_at).total_seconds() * 1000))
        page = self.updating_page.replace(
            "/*__AVA_DEPLOY_STARTED_AT__*/", json.dumps(snapshot.started_at.isoformat())
        )
        page = page.replace("/*__AVA_UPDATE_BASE_ELAPSED_MS__*/", str(base_ms))
        self._serve_static(handler, page, 503)

    @staticmethod
    def serve_deploy_snapshot(handler: BaseHTTPRequestHandler, snapshot: UiUpdateSnapshot) -> None:
        """Same-origin reload hint for an already-open SPA.

        Minimal on purpose: the browser needs only whether to reload. The Gate
        itself remains the owner of classification, timing and diagnostics.
        """
        body = json.dumps(
            {"status": snapshot.status, "generation": snapshot.generation},
            separators=(",", ":"),
        ).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _serve_static(handler: BaseHTTPRequestHandler, page: str, status: int = 200) -> None:
        body = page.encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


class _Handler(BaseHTTPRequestHandler):
    """One request: probe the gateway, then serve login / updating / proxy.

    `gate` is set on the server instance (`server.gate`); the handler reads it
    via `self.server`."""

    @property
    def gate(self) -> Gate:
        return self.server.gate  # type: ignore[attr-defined,no-any-return]  # set in main()/tests

    def do_GET(self) -> None:
        self._handle()
        self._maybe_close_connection()

    def do_POST(self) -> None:
        self._handle()
        self._maybe_close_connection()

    def _handle(self) -> None:
        cookie = self.headers.get("Cookie") or ""
        snapshot = self.gate.deploy_snapshot()
        if urlsplit(self.path).path == "/__ava/deploy-state":
            if self.command != "GET":
                self.send_response(405)
                self.send_header("Allow", "GET")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.gate.serve_deploy_snapshot(self, snapshot)
            return
        state = self.gate.verdict(cookie, snapshot)
        if state == "up":
            self.gate.proxy_app(self, snapshot)
        elif state == "login":
            self.gate._serve_static(self, self.gate.login_page)
        elif state == "updating":
            self.gate.serve_maintenance(self, snapshot)
        else:
            self.gate._serve_static(self, self.gate.down_page, 503)

    def _maybe_close_connection(self) -> None:
        # urllib may have consumed the request body; keep-alive is not worth the
        # edge cases — the gate's clients are browsers hitting a few assets.
        self.close_connection = True

    def log_message(
        self, format: str, *args: object
    ) -> None:  # parameter name must match http.server
        _log.debug(format, *args)


def entry_port() -> int:
    """The port the gate binds — the fleet UI entry (the `frontend` slot).

    Public because the operator surfaces probe it (`cli.commands._converge_gate.
    probe_gate`), and a monitor that derives the entry port for itself is a second
    definition that can disagree with the one the gate binds.
    """
    return urlsplit(settings.services.frontend_healthcheck_url).port or 3000


def app_port() -> int:
    """The Next.js app port the gate proxies to — `app` = entry + 1."""
    return settings.services.app_port or (entry_port() + 1)


def _app_base() -> str:
    """The Next.js app base the gate proxies to."""
    return f"http://127.0.0.1:{app_port()}"


def _gateway_base() -> str:
    """The gateway API base the gate probes and the login page posts to.

    The host must be one the user's browser can dial: the login page posts the
    cluster secret to this origin, and a remote browser resolving `127.0.0.1`
    would hit itself. `reachable_host()` (AVA_MACHINE_HOST >
    `$AVA_HOME/machine_host` > `localhost`) is the same operator-declared
    address `ava.ui.show` hands the user for direct page URLs and the bootstrap
    rewrites loopback data-plane URLs to; a single box falls back to
    `localhost` unchanged. The server-side auth probe follows the same base —
    on a gateway host the gateway binds all interfaces, so dialing the host's
    own reachable address is equivalent to loopback and keeps one origin
    everywhere, mirroring how remote agent-runners dial the gateway.
    """
    from shared.machine import reachable_host

    host = reachable_host()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6 literal — netloc needs brackets
    return f"http://{host}:{settings.gateway.gateway_port}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Always-up entry to the fleet UI")
    parser.add_argument("--port", type=int, default=None, help="bind port (default: entry port)")
    parser.add_argument("--static-dir", type=Path, default=None, help="static pages directory")
    args = parser.parse_args()

    from shared.daemon_shutdown import install_graceful_shutdown
    from shared.log import init_gateway_process

    init_gateway_process("gate")
    # launchd stops this one (it is outside the session roster and the update
    # lifecycle), and launchd's stop is also SIGTERM: without a handler the
    # default disposition kills it mid-request instead of letting serve_forever
    # return and close the listener. Nothing asyncio about the helper — raising
    # in the handler breaks the blocking accept() the same way Ctrl-C does.
    install_graceful_shutdown("gate")
    static_dir = args.static_dir or (Path(__file__).parent / "static")
    port = args.port or entry_port()
    gate = Gate(
        gateway_base=_gateway_base(),
        app_base=_app_base(),
        static_dir=static_dir,
        state_path=None,
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)  # noqa: S104 — the entry must be reachable off-box
    server.gate = gate  # type: ignore[attr-defined]  # handler reads it off the instance
    _log.info("gate serving on :%d (app %s, gateway %s)", port, gate.app_base, gate.gateway_base)
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
