"""Listener discovery — who holds a TCP port on this host.

The storage ownership checks (`base.cluster.ownership`), the PgBouncer bind wait
and the OTLP collector healthcheck need the same facts about a port:

- `strict_listeners_on` — the pids listening on a port, raising
  `ListenerDiscoveryError` when the socket table cannot establish absence (psutil
  first; lsof when macOS denies the global scan; two disagreeing inspections prove
  nothing);
- `listener_addrs` — the local addresses listening on a port, best-effort.
"""

from __future__ import annotations

import os
import shutil

import base.host.proc


class ListenerDiscoveryError(RuntimeError):
    """The socket table could not establish listener ownership."""


# Restricted contexts (cron, forced ssh commands, minimal login shells)
# carry a PATH that often omits the platform's lsof location — macOS keeps
# lsof in /usr/sbin, which cron's /usr/bin:/bin PATH never reaches — so PATH
# lookup alone cannot be trusted. PATH wins when it resolves (fast, and it
# honours an operator's own binary), then these candidates, in order.
_LSOF_CANDIDATE_PATHS = (
    "/usr/sbin/lsof",  # macOS and most Linux distros
    "/usr/bin/lsof",
    "/sbin/lsof",
    "/bin/lsof",
    "/usr/local/bin/lsof",
    "/usr/local/sbin/lsof",
    "/opt/homebrew/bin/lsof",  # Homebrew on Apple Silicon
)


def _lsof_argv(*args: str) -> list[str] | None:
    """Resolved lsof argv for `args`, or None when no lsof is reachable.

    None means the inspection cannot run at all — the caller reports a
    discovery failure rather than reading the missing inspection as absence.
    """
    binary = shutil.which("lsof")
    if binary is None:
        for candidate in _LSOF_CANDIDATE_PATHS:
            if os.access(candidate, os.X_OK):
                binary = candidate
                break
    if binary is None:
        return None
    return [binary, *args]


def _psutil_listeners_on(port: int) -> tuple[list[int], bool] | None:
    """`(pids, conclusive)` from the psutil scan, or None when the scan failed.

    conclusive=True means the table was read and every LISTEN socket on
    `port` was attributed — including the empty case, where `port` is absent
    from the table. conclusive=False means a LISTEN socket on `port` exists
    but psutil could not name its pid (the `pid is None` rows a restricted
    /proc or socket table yields) — the caller must not read that as absence.
    """
    import psutil

    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return None
    pids: list[int] = []
    for conn in conns:
        if conn.status != "LISTEN":
            continue
        addr = conn.laddr
        if len(addr) < 2:
            continue
        if addr[1] == port:  # psutil addr = (ip, port); `addr.port` is
            if conn.pid is None:  # un-narrowable against `tuple[()]`
                return [], False
            pids.append(conn.pid)
    return list(dict.fromkeys(pids)), True


def _lsof_listeners_on(port: int) -> list[int]:
    """Listener PIDs from lsof, raising ListenerDiscoveryError on failure.

    lsof's exit 1 with no diagnostics is its "no match" answer — genuine
    absence. Anything else (unreachable binary, timeout, non-empty stderr,
    or exit 1 WITH output) is an inspection failure the caller must not
    read as absence.
    """
    import subprocess

    argv = _lsof_argv("-nP", "-Fp", "-sTCP:LISTEN", f"-iTCP:{port}")
    if argv is None:
        raise ListenerDiscoveryError(
            f"listener discovery failed on port {port}: lsof is not on PATH "
            "and not in the standard locations"
        )
    try:
        # S603: static argv; the only interpolated piece is an int port.
        out = base.host.proc.run_bounded(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ListenerDiscoveryError(f"listener discovery failed on port {port}: {exc}") from exc
    if out.returncode not in (0, 1) or out.stderr.strip() or (out.returncode == 1 and out.stdout):
        raise ListenerDiscoveryError(
            f"listener discovery failed on port {port}: lsof exit {out.returncode}: {out.stderr.strip()}"
        )
    pids: list[int] = []
    for line in out.stdout.splitlines():
        if line.startswith("p"):
            try:
                pids.append(int(line[1:]))
            except ValueError:
                continue
    return list(dict.fromkeys(pids))


def strict_listeners_on(port: int) -> list[int]:
    """Return listener PIDs, raising when discovery cannot establish absence.

    The psutil scan is trusted only when conclusive: a read failure (macOS
    denies the global scan) or an unattributed LISTEN socket both fall back
    to lsof. A psutil sighting that lsof cannot confirm is an inspection
    failure — two disagreeing inspections prove nothing, and "saw a
    listener, cannot name it" must never be reported as "nothing listens".
    """
    scan = _psutil_listeners_on(port)
    if scan is not None and scan[1]:
        return scan[0]
    lsof_pids = _lsof_listeners_on(port)
    if scan is not None and not lsof_pids:
        raise ListenerDiscoveryError(
            f"listener discovery failed on port {port}: the socket table shows "
            "a listener that neither psutil nor lsof could attribute to a pid"
        )
    return lsof_pids


def listener_addrs(port: int) -> set[str]:
    """Local addresses with a TCP listener on `port`, best-effort.

    psutil is the primary, cross-platform socket-table scan. As with
    `listeners_on`, macOS may reject the whole scan when any process is
    unreadable, so POSIX hosts fall back to lsof's machine-readable name fields.
    An empty set means no listener address could be proven — either none exists
    or both inspection paths failed — and callers must treat that uncertainty
    conservatively rather than as proof that no listener exists.
    """
    import psutil

    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        conns = None
    if conns is not None:
        addrs: set[str] = set()
        for conn in conns:
            if conn.status != "LISTEN":
                continue
            addr = conn.laddr
            if len(addr) < 2:
                continue
            if addr[1] == port:
                addrs.add(addr[0])
        return addrs

    import subprocess

    argv = _lsof_argv("-nP", "-Fpn", "-sTCP:LISTEN", f"-iTCP:{port}")
    if argv is None:
        return set()
    try:
        # S603: static argv; the only interpolated piece is an int port.
        out = base.host.proc.run_bounded(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if out.returncode != 0:
        return set()

    addrs = set()
    for line in out.stdout.splitlines():
        if not line.startswith("n"):
            continue
        host, separator, port_text = line[1:].rpartition(":")
        if not separator:
            continue
        try:
            if int(port_text) != port:
                continue
        except ValueError:
            continue
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if host:
            addrs.add(host)
    return addrs


LOOPBACK_ALIASES = frozenset({"127.0.0.1", "::1", "localhost", "ip6-localhost"})


def bind_addrs(cluster_secret: str) -> list[str]:
    """Loopback plus this host's reachable address, de-duplicated (loopback alone
    when reachable resolves to localhost — the single-box default).

    A no-secret cluster binds LOOPBACK ONLY, whatever the reachable address says:
    an empty secret is the single-box posture (its API and `/ops` serve
    unauthenticated and no other machine dials its data plane), so Postgres and
    its pooler, though they always authenticate (SCRAM), have no reason to face
    the LAN. The bearer decides the network posture — an operator who wants a
    LAN-reachable Postgres data plane sets the cluster secret.

    `cluster_secret` is the CALLER-PASSED cluster secret (the same value the hba
    is written from and the pooler is configured with), never read from
    `settings` — a process that inherited a sibling cluster's
    AVA_CLUSTER_SECRET (a shell carrying a different home's environment) must not widen
    a no-secret cluster's bind posture to the LAN. The caller resolves the
    cluster's own secret from its authority-passed `.env` value."""
    if not cluster_secret:
        return ["127.0.0.1"]
    from base.cluster import machine

    host = machine.reachable_host()
    out = ["127.0.0.1"]
    if host not in LOOPBACK_ALIASES:
        out.append(host)
    return out
