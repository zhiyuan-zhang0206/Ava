"""Rewrite the identity parts of a data-plane URL's netloc — stdlib only, so it
is safe to import during the Settings load (shared.config) and from
shared.cluster.

Names-as-data (path-only identity): the username / database a URL carries ARE
the cluster's data-plane identifiers — they are never derived from a name, so
Settings only re-applies the PASSWORD (the cluster secret, `url_with_password`)
on load. `url_with_userinfo` sets the full identity and is used at birth, when
the identifier is chosen (`shared.cluster.DATA_PLANE_IDENTITY`).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


def url_with_userinfo(url: str, user: str, password: str) -> str:
    """Return `url` with its userinfo set to `user:password` (`scheme://user:pw@host`;
    `scheme://:pw@host` when `user` is empty, as the redis default user has no name).

    Both components are percent-encoded so a URL-reserved character cannot break
    the netloc. An empty `password` still writes the username (`user@host`) — the
    username IS the data-plane identity (names-as-data), and a no-secret cluster
    carries identity without a credential. `url_with_password` is the password-only
    rewrite and keeps its no-op-on-empty contract."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal — urlsplit stripped the [...] brackets
        host = f"[{host}]"
    userinfo = f"{quote(user, safe='')}"
    if password:
        userinfo += f":{quote(password, safe='')}"
    netloc = f"{userinfo}@{host}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def url_with_password(url: str, password: str) -> str:
    """Return `url` with its userinfo password replaced by `password`, keeping the
    existing username. Thin wrapper over url_with_userinfo for callers that only
    rotate the secret and leave the username as-is."""
    if not password:
        return url
    return url_with_userinfo(url, urlsplit(url).username or "", password)


def url_with_port(url: str, port: int) -> str:
    """Return `url` with its netloc port replaced by `port`, keeping
    scheme/userinfo/host/path/query. Used by the PgBouncer dial switch: the pooled
    URL is the cluster's own db_url with only the port swapped from the real
    Postgres port to the co-located PgBouncer listener (same host, same
    userinfo/db), so the re-derived per-cluster identity still reaches the wire —
    just through the pooler."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal — urlsplit stripped the [...] brackets
        host = f"[{host}]"
    userinfo = ""
    if parts.username is not None:
        userinfo = quote(parts.username, safe="")
        if parts.password is not None:
            userinfo += f":{quote(parts.password, safe='')}"
        userinfo += "@"
    netloc = f"{userinfo}{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def url_with_host(url: str, host: str) -> str:
    """Return `url` with its netloc host replaced by `host`, keeping
    scheme/userinfo/port/path/query. The userinfo substring is carried over
    verbatim from the original netloc — no reparse / re-quote — so an already
    percent-encoded password round-trips byte-identically (re-quoting would
    double-encode it and change the credential bytes before the dial). An IPv6
    replacement host is bracketed.

    Used by the self-dial loopback rewrite (shared.config.data_plane): when a
    data-plane URL names this machine's own reachable address, the dial host is
    swapped to loopback while identity/port/database stay exactly as derived."""
    parts = urlsplit(url)
    if ":" in host and not host.startswith("["):  # IPv6 literal — netloc needs [...]
        host = f"[{host}]"
    # rpartition on "@" matches urlsplit's own userinfo/hostport split.
    userinfo, sep, _hostport = parts.netloc.rpartition("@")
    netloc = f"{userinfo}{sep}{host}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def url_with_query_param(url: str, key: str, value: str) -> str:
    """Return `url` with its query string's `key` set to `value` (added if
    absent, replaced in place if already present); scheme/userinfo/host/
    port/path and every other query param pass through untouched.

    Used to set libpq's `hostaddr` (shared.config.data_plane) — a query
    param, not a netloc part, so `url_with_host`/`url_with_port` don't cover
    it.
    """
    parts = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    params.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))
