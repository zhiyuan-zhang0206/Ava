"""httpx dials that pin an IPv4-literal target to AF_INET, bypassing
hostname resolution entirely.

Background — see `shared.netutil.is_ipv4_literal` for the network failure
mode this defends against (DNS64/NAT64 synthesis of a v4 literal). This
module is the httpx-specific fix; Postgres gets the equivalent via libpq's
own `hostaddr=` (`shared/config/data_plane.py`).

Only the SYNC path needs it. httpx's async transport (httpcore's
`AnyIOBackend`, backed by `anyio.connect_tcp`) and `redis.asyncio` (via
stdlib `asyncio`'s `BaseEventLoop._ensure_resolved`) both already check
`ipaddress.ip_address(host)` before ever calling `getaddrinfo` — confirmed by
counting `socket.getaddrinfo` calls under a literal target: 0 for both. Only
httpcore's `SyncBackend` (a thin wrapper over stdlib
`socket.create_connection`, which calls `getaddrinfo` unconditionally) and
redis-py's sync `Connection._connect` lack that check; redis-py's fix lives
in `shared/redis_client.py` (a custom `Connection` subclass, mirroring the
same idea).

httpx.HTTPTransport doesn't expose httpcore.ConnectionPool's `network_backend`
parameter — the one hook this needs — so `PinnedIPv4Transport` swaps
`self._pool`'s network backend in-place right after the stock constructor
builds it. `ConnectionPool._network_backend` is read fresh, lazily, whenever
a connection is actually created (`create_connection`), never cached
elsewhere at construction time, so mutating it before the first request is
made is equivalent to having passed it in. This keeps httpx's own
request/response translation and httpcore-exception-to-httpx-exception
mapping (`map_httpcore_exceptions`) intact — reimplementing `handle_request`
from scratch would also have to reimplement that mapping to keep callers'
`except httpx.TransportError` / `except httpx.ConnectTimeout` handling
working, which is the whole reason this reuses `httpx.HTTPTransport` rather
than building a transport from bare httpcore.
"""

from __future__ import annotations

import socket
import typing
from urllib.parse import urlsplit

import httpcore
import httpx

from shared.netutil import is_ipv4_literal


class _PinnedStream(httpcore.NetworkStream):
    """An already-connected AF_INET socket, wrapped for httpcore.

    Mirrors `httpcore._backends.sync.SyncStream`'s read/write/close/
    start_tls contract (that class is private, so this is a small
    from-scratch reimplementation, not a subclass) including its exception
    mapping — a `socket.timeout` becomes `httpcore.{Read,Write}Timeout`, any
    other `OSError` becomes `httpcore.{Read,Write}Error` — so httpx's own
    `map_httpcore_exceptions` in `HTTPTransport.handle_request` converts them
    to `httpx.ReadTimeout` / `httpx.ReadError` etc. exactly as it would for
    the stock backend.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            self._sock.settimeout(timeout)
            return self._sock.recv(max_bytes)
        except TimeoutError as exc:
            raise httpcore.ReadTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore.ReadError(str(exc)) from exc

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if not buffer:
            return
        try:
            while buffer:
                self._sock.settimeout(timeout)
                sent = self._sock.send(buffer)
                buffer = buffer[sent:]
        except TimeoutError as exc:
            raise httpcore.WriteTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore.WriteError(str(exc)) from exc

    def close(self) -> None:
        self._sock.close()

    def start_tls(
        self,
        ssl_context: typing.Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        try:
            self._sock.settimeout(timeout)
            wrapped = ssl_context.wrap_socket(self._sock, server_hostname=server_hostname)
        except Exception as exc:
            self.close()
            raise httpcore.ConnectError(str(exc)) from exc
        return _PinnedStream(wrapped)

    def get_extra_info(self, info: str) -> typing.Any:
        if info == "client_addr":
            return self._sock.getsockname()
        if info == "server_addr":
            return self._sock.getpeername()
        if info == "socket":
            return self._sock
        return None


class _PinnedIPv4Backend(httpcore.SyncBackend):
    """`httpcore.SyncBackend` whose `connect_tcp` bypasses `getaddrinfo` for
    an IPv4-literal `host`, connecting directly over `AF_INET`. A host that
    is not a literal (a hostname, or an IPv6 literal) falls through to
    `super().connect_tcp` unchanged — this only ever changes behavior for the
    one case `is_ipv4_literal` matches."""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        if not is_ipv4_literal(host):
            return super().connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            if local_address is not None:
                sock.bind((local_address, 0))
            sock.connect((host, port))
        except TimeoutError as exc:
            sock.close()
            raise httpcore.ConnectTimeout(str(exc)) from exc
        except OSError as exc:
            sock.close()
            raise httpcore.ConnectError(str(exc)) from exc
        for option in socket_options or ():
            # SOCKET_OPTION is a 3-tuple (level, optname, int|buffer value) or a
            # 4-tuple (level, optname, None, buflen) -- socket.setsockopt's two
            # overloads, matched by length since a bare `*option` unpack blurs
            # both shapes into one union pyright can't match against either.
            if len(option) == 3:
                level, optname, value = option
                sock.setsockopt(level, optname, value)
            else:
                level, optname, _none, buflen = option
                sock.setsockopt(level, optname, None, buflen)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return _PinnedStream(sock)


class PinnedIPv4Transport(httpx.HTTPTransport):
    """`httpx.HTTPTransport` that pins IPv4-literal targets to a direct
    `AF_INET` connect (see module docstring). Drop-in for `HTTPTransport()`
    — takes no extra arguments since every call site here builds one with
    defaults (no proxy, no client cert); extend the signature if a future
    caller needs those."""

    def __init__(self) -> None:
        super().__init__()
        if not isinstance(self._pool, httpcore.ConnectionPool):
            # Only the no-proxy branch of HTTPTransport.__init__ builds a
            # plain ConnectionPool; cluster-internal dials never go through
            # an HTTP/SOCKS proxy, so this should be unreachable. Fail loud
            # rather than silently leaving the pin inactive.
            raise TypeError(
                f"httpx.HTTPTransport._pool is a {type(self._pool).__name__}, not "
                "httpcore.ConnectionPool -- PinnedIPv4Transport's network_backend "
                "swap has no effect; check it against the installed httpx version"
            )
        self._pool._network_backend = _PinnedIPv4Backend()


def transport_for_url(url: str) -> httpx.HTTPTransport | None:
    """`PinnedIPv4Transport()` when `url`'s host is an IPv4 literal, else
    `None` (httpx's own default transport — unchanged behavior for a
    hostname target)."""
    host = urlsplit(url).hostname or ""
    return PinnedIPv4Transport() if is_ipv4_literal(host) else None


def get(url: str, **kwargs: typing.Any) -> httpx.Response:
    """`httpx.get` for a cluster-internal dial: pins an IPv4-literal `url`
    host, otherwise identical — literally `httpx.get` for a hostname target
    (including test suites that `monkeypatch.setattr("httpx.get", ...)`;
    that keeps working since this calls the real thing). `kwargs` forwards
    to `Client.get` (params / headers / cookies / auth / follow_redirects /
    timeout / extensions) — matches every call site this repo has today; a
    caller needing a Client-construction-level kwarg (`verify` / `proxy` /
    `trust_env`) should build its own
    `httpx.Client(transport=transport_for_url(url), ...)` instead."""
    transport = transport_for_url(url)
    if transport is None:
        return httpx.get(url, **kwargs)
    with httpx.Client(transport=transport) as client:
        return client.get(url, **kwargs)


def post(url: str, **kwargs: typing.Any) -> httpx.Response:
    """`httpx.post` for a cluster-internal dial. See `get`."""
    transport = transport_for_url(url)
    if transport is None:
        return httpx.post(url, **kwargs)
    with httpx.Client(transport=transport) as client:
        return client.post(url, **kwargs)


def put(url: str, **kwargs: typing.Any) -> httpx.Response:
    """`httpx.put` for a cluster-internal dial. See `get`."""
    transport = transport_for_url(url)
    if transport is None:
        return httpx.put(url, **kwargs)
    with httpx.Client(transport=transport) as client:
        return client.put(url, **kwargs)


def patch(url: str, **kwargs: typing.Any) -> httpx.Response:
    """`httpx.patch` for a cluster-internal dial. See `get`."""
    transport = transport_for_url(url)
    if transport is None:
        return httpx.patch(url, **kwargs)
    with httpx.Client(transport=transport) as client:
        return client.patch(url, **kwargs)


def delete(url: str, **kwargs: typing.Any) -> httpx.Response:
    """`httpx.delete` for a cluster-internal dial. See `get`."""
    transport = transport_for_url(url)
    if transport is None:
        return httpx.delete(url, **kwargs)
    with httpx.Client(transport=transport) as client:
        return client.delete(url, **kwargs)
