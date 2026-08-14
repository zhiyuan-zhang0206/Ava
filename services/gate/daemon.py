"""The gate — the always-up entry to the fleet UI.

Owns the entry port (the port the user bookmarks; `frontend` in the port
table). Serves the static login page and two maintenance pages — "updating"
and "service unavailable" — and proxies the Next.js app (on `app` = entry+1)
whenever the cluster is healthy. A rollout takes the gateway down (503) and restarts the frontend —
the gate is a launchd job OUTSIDE the update lifecycle, so the entry never
blacks out: users see the updating page during the rollout and land back on
the app automatically when it finishes.

Auth is the gateway's session cookie (host-only, shared across ports): the
gate forwards the client's Cookie to `GET /api/auth/check` and serves the
static login page when unauthenticated, the app when authenticated, and one
of two maintenance pages whenever the gateway is unreachable or 503. Which
one is decided by the posture mirror (`$AVA_HOME/deploy-state.json`): a
rollout/update marks it before the gateway goes down and clears it when the
host serves again (the posture mirror), so a non-idle mirror means
"updating" and the flag absent means the machine itself is unreachable —
powered off, crashed, or partitioned — a genuinely different thing that the
old single page could not say.

The SPA's own API/SSE traffic goes straight to the gateway (never through
this proxy), so a simple buffering proxy suffices — no streaming needed.

All three static pages are rebuilds of the app's own screens
(`login/page.tsx`, `updating-page.tsx`) from a copy of its design tokens,
down to reading the theme the user picked in the app — a rollout swaps one
for the other, and
that should not look like landing on a different product. They stay
dependency-free: no build output, no network fetch, everything inlined.
"""

from __future__ import annotations

import argparse
import json
import logging
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from shared.config import settings

_log = logging.getLogger("services.gate")

# Headers forwarded to the app / gateway on proxied requests. Everything else
# (Host, hop-by-hop, cookies beyond the session one) is dropped — the app and
# the auth check do not need them.
_FORWARD_HEADERS = ("accept", "accept-language", "cookie", "content-type")

_PROBE_TIMEOUT_S = 3.0


class Gate:
    """Request-agnostic gate behavior; the HTTP handler is a thin shell."""

    def __init__(
        self,
        *,
        gateway_base: str,
        app_base: str,
        static_dir: Path,
        mirror_path: str | None = None,
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
        # The mirror file tells the two maintenance pages apart (R1, Task
        # #1021): `$AVA_HOME/deploy-state.json` is the host-local projection of
        # the posture row, written by the same transitions that write the DB.
        # A path is injected (tests pass a tmp file); the default resolves from
        # the same AVA_HOME the rollout orchestration writes to — the gate and
        # the gateway are always on one machine, so the file is always local.
        self.mirror_path = mirror_path

    def verdict(self, cookie: str) -> str:
        """ "up" (authenticated, cluster healthy) | "login" (healthy, no session)
        | "updating" (gateway unreachable / 503 while the posture mirror is non-idle)
        | "down" (gateway unreachable / 503 with no flag — the machine itself
        | is not answering)."""
        req = urllib.request.Request(  # noqa: S310 — operator-declared internal URL, never request input
            f"{self.gateway_base}/api/auth/check",
            headers={"Cookie": cookie} if cookie else {},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 — operator-declared internal URL
                req, timeout=_PROBE_TIMEOUT_S
            ) as resp:
                body = json.loads(resp.read())
                return "up" if body.get("authenticated") else "login"
        except Exception:  # any probe failure means "not serving"; never crash a request
            # The mirror file separates the two things "not serving" can mean: the
            # pause transition wrote it (posture != idle) before the gateway went
            # down, so a non-idle mirror says "updating", and an absent / idle
            # mirror says the machine is unreachable (R1, Task #1021 — replaces
            # the old updating.flag existence read).
            from shared.host_deploy_state import read_mirror

            mirror = read_mirror(self.mirror_path)
            posture = mirror.get("posture") if mirror else None
            return "updating" if posture not in (None, "idle") else "down"

    def proxy_app(self, handler: BaseHTTPRequestHandler) -> None:
        """Fetch the path from the app and stream it back; on failure serve the
        updating page (the app may be mid-rebuild during a rollout).

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
                if value:
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
                if name.lower() in ("content-type", "cache-control", "etag"):
                    handler.send_header(name, value)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
        except Exception:  # transport failure — app down/rebuilding is the maintenance case
            _log.warning("app proxy failed for %s", handler.path)
            self._serve_static(handler, self.updating_page, 502)

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
        state = self.gate.verdict(cookie)
        if state == "up":
            self.gate.proxy_app(self)
        elif state == "login":
            self.gate._serve_static(self, self.gate.login_page)
        elif state == "updating":
            self.gate._serve_static(self, self.gate.updating_page, 503)
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
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)  # noqa: S104 — the entry must be reachable off-box
    server.gate = gate  # type: ignore[attr-defined]  # handler reads it off the instance
    _log.info("gate serving on :%d (app %s, gateway %s)", port, gate.app_base, gate.gateway_base)
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
