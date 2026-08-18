"""Is the browser on this cluster's CDP port *ours*?

Every other Ava service answers a `/healthz` carrying `name`/`home`/`pid`, and
`shared.daemon_health.probe_daemon` refuses to call a 200 healthy until those
identify this unit's own daemon. That check exists because a pytest-leaked
restarter on prod's default port kept a healthcheck green for 98 minutes while
the real restarter was dead.

The browser had no equivalent. It is probed by dialling Chrome's DevTools
Protocol (`/json/version`), which is not ours to extend: **CDP carries no field
we control**. Measured on Chrome 150 rather than assumed — `/json/version`
returns `Browser`, `Protocol-Version`, `User-Agent`, `V8-Version`,
`WebKit-Version` and a `webSocketDebuggerUrl` whose uuid is minted per launch and
recorded nowhere we can read back. (`DevToolsActivePort`, the file Chrome writes
into the user-data dir and which *would* have tied the endpoint to the profile,
is written only for an auto-assigned port; with an explicit
`--remote-debugging-port` it is absent. Verified against this cluster's own
profile and a throwaway one.) So a status-code probe answers "a debuggable Chrome
is up", never "this unit's browser is up", and any Chrome that got the port first
— including the WSL2 unit's, relayed onto a Windows unit's loopback — reads green.
Agents then drive the wrong browser, which is worse than a red check because
nothing looks wrong.

## Identity comes from the profile, and the port ties it to the answer

Ava launches Chrome with `--user-data-dir=$AVA_HOME/chrome-profile`, a path
minted per cluster under a home that belongs to this install alone.
`services/browser/orphan.py` already established that token as a strictly
positive identifier — no Chrome that is not ours is launched with it — and
`find_cluster_chrome` is the process-table walk that finds it. That is the
`home` arm of the identity check, and this module reuses it rather than
restating it.

The `pid` arm is the missing half: a Chrome of ours *existing* is not evidence
that it is the process answering the port. The WSL case is exactly that shape —
the two units are in different network namespaces, so the relay and this unit's
Chrome can both be alive while only one of them owns the Windows loopback port,
and a Chrome that lost the bind keeps running with its DevTools endpoint dead.
So the conjunction is: CDP answers on the port **and** a Chrome on this cluster's
profile holds a LISTEN socket on that same port. `psutil` supplies the socket
list for a same-user process on all three platforms without elevation.

The two arms are checked in BOTH directions. The walk direction
(`find_cluster_chrome` then per-pid socket reads) alone has structural blind
spots: a Chrome whose argv the walk could not read, or whose per-pid socket
list failed, would be read as "another unit's browser holds this port" while
our own Chrome is the listener — the exact false-terminal class the win box
field-diagnosis chased on 2026-08-11. The listener-first direction answers the
same question from the other end: read the global TCP table once
(`_listener_pid`), and if the pid owning the LISTEN socket is a Chrome on this
cluster's profile — by the walk, or by the holder's own argv when the walk
missed it — the browser is alive, full stop. Either direction can win; only a
positive identification reads ALIVE, and an unreadable table falls back to the
walk so the gain never becomes a dependency.

## What it does not answer

Only what runs on THIS machine. `find_cluster_chrome` walks this host's process
table, so the probe is machine-local by construction — which is the right scope
(`ava status`, the health probe and the watchdog all run beside the browser they
are asking about) but means a remote caller cannot use it.

Nothing here supervises. A Chrome that is ours and holds the port can still have
left the `ava-browser` session on a `SingletonLock` handoff; identity and
supervision are two questions and `services/healthchecks/browser.py` asks both.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from pathlib import Path

import psutil

from shared.config import settings
from shared.daemon_health import DaemonProbe

from .orphan import find_cluster_chrome, is_cluster_chrome
from .profile import profile_dir

_log = logging.getLogger("services.browser.probe")

# CDP is a loopback JSON dump; anything slower is a wedged browser, which is
# "dead" for watchdog purposes. Matches the healthcheck timeout it replaces.
_CDP_TIMEOUT_S = 3.0

# psutil exceptions meaning "gone, or not ours to read" — the same set
# `orphan.py` skips on, for the same reason: a process we cannot read is one we
# cannot identify.
_UNREADABLE = (psutil.NoSuchProcess, psutil.AccessDenied, OSError)


def cdp_url(port: int) -> str:
    """The CDP version endpoint for `port` — the one HTTP surface Chrome serves
    without a websocket. Single source for the daemon's port guard, the
    healthcheck and this probe."""
    return f"http://127.0.0.1:{port}/json/version"


def _cdp_unreachable(port: int) -> str | None:
    """None when a debuggable Chrome answers 200 on `port`; else why it did not.

    Deliberately does not say *whose* Chrome — that is the next question, and
    keeping the two separate is what lets a foreign occupant be reported as a
    different condition from a dead browser.
    """
    url = cdp_url(port)
    try:
        with urllib.request.urlopen(url, timeout=_CDP_TIMEOUT_S) as resp:  # noqa: S310 — fixed loopback CDP URL
            if resp.status != 200:
                return f"CDP {url} returned HTTP {resp.status}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"CDP unreachable on {url}: {type(exc).__name__}: {exc}"
    return None


def _process_facts(pid: int) -> tuple[str, list[str] | None]:
    """`(name, argv)` for `pid`, with argv None when it cannot be read — the same
    shape `orphan._readable_facts` uses, so a holder that cannot be identified is
    reported, never guessed at."""
    try:
        proc = psutil.Process(pid)
        return proc.name(), proc.cmdline()
    except _UNREADABLE:
        return "", None


def _listener_pid(port: int) -> int | None:
    """The pid owning the LISTEN socket on `port`, from the global TCP table;
    None when the table cannot be read or nobody listens.

    The listener-first direction of the identity check: whoever owns the LISTEN
    socket is the process the CDP dial actually reaches, so this sidesteps both
    failure modes of the profile-walk direction (a Chrome our walk missed, and a
    per-pid socket read that failed). One table read covers every process, so no
    per-pid handle is needed; on platforms where the table is root-only for
    foreign processes the read still yields our own Chrome's entry, and a failed
    read falls back to the walk direction.
    """
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return None
    for conn in conns:
        if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr[1] == port:
            return conn.pid
    return None


def _listens_on(pid: int, port: int) -> bool | None:
    """Whether `pid` holds a LISTEN socket on `port`; None when its sockets cannot
    be read.

    None is not False. "Chrome is not the listener" and "we could not look" are
    different facts, and only the first is evidence — the caller reports the
    second as unverified rather than folding it into either verdict.
    """
    try:
        conns = psutil.Process(pid).net_connections(kind="tcp")
    except _UNREADABLE:
        return None
    return any(c.status == psutil.CONN_LISTEN and c.laddr and c.laddr[1] == port for c in conns)


def probe_browser(port: int | None = None, profile: Path | None = None) -> DaemonProbe:
    """Probe the shared headed browser and verify the Chrome answering is ours,
    ALWAYS returning a verdict.

    The total wrapper (the probe contract every healthcheck obeys); `_probe_browser`
    is the probe. Fails closed: an unforeseen failure is reported down, never alive.
    """
    try:
        return _probe_browser(port, profile)
    except Exception as exc:
        _log.exception("[probe_browser] probe raised unexpectedly; treating as down")
        return DaemonProbe.down(f"probe raised {type(exc).__name__}: {exc}")


def _probe_browser(port: int | None, profile: Path | None) -> DaemonProbe:
    """CDP answers on this cluster's port AND a Chrome on this cluster's profile is
    the process listening there.

    The three verdicts map onto `ProbeVerdict` the same way every other service's
    do — the line is whether a respawn can fix it:

    - **CDP unreachable** → `DOWN`. No browser (or a hung one); `respawn_service`
      kills the stale session and relaunches, which is exactly the fix.
    - **CDP answers, but no Chrome of this cluster's profile listens there** →
      `PORT_TAKEN`, terminal. The occupant is another unit's browser or a Chrome
      on someone else's profile, and `services/browser/daemon.py` refuses to
      launch while the port is served — so a respawn is not remediation, it is a
      crash loop once every 60s. An operator has to free the port.
    - **both hold** → `ALIVE`, with the pid in the detail.
    """
    port = settings.services.browser_cdp_port if port is None else port
    profile = profile_dir() if profile is None else profile

    unreachable = _cdp_unreachable(port)
    if unreachable is not None:
        return DaemonProbe.down(unreachable)

    found = find_cluster_chrome(profile)

    # Listener-first: the process that owns the LISTEN socket is the one CDP
    # answers from. If it is ours — by the walk, or by its own argv when the
    # walk missed it — the browser is alive, full stop. This direction is what
    # turns the walk's blind spots into a positive identification instead of a
    # false terminal.
    holder = _listener_pid(port)
    if holder is not None and holder in found:
        return DaemonProbe.up(f"chrome pid {holder} on {profile}")
    if holder is not None:
        holder_name, holder_argv = _process_facts(holder)
        if is_cluster_chrome(holder_name, holder_argv, profile):
            return DaemonProbe.up(f"chrome pid {holder} on {profile}")

    unreadable: list[int] = []
    for pid in found:
        listening = _listens_on(pid, port)
        if listening:
            return DaemonProbe.up(f"chrome pid {pid} on {profile}")
        if listening is None:
            unreadable.append(pid)
    if unreadable:
        # Terminal, not down, and deliberately so: CDP *is* being served, so a
        # respawn meets the daemon's port guard whoever owns it. Reporting the
        # unread pids beats guessing in either direction.
        return DaemonProbe.port_taken(
            f"CDP :{port} answers, but the listening sockets of this cluster's Chrome "
            f"{unreadable} could not be read — cannot confirm it owns the port"
        )
    if found:
        # The shape the win runner met 2026-08-11: our Chrome IS running — it
        # lost the bind (the relay / another unit's Chrome won it) and serves a
        # dead DevTools endpoint while a foreign CDP answer comes from the port.
        # Naming the pids is what makes the log line self-explanatory instead of
        # reading like "your Chrome does not exist".
        return DaemonProbe.port_taken(
            f"identity mismatch on CDP :{port}: this cluster's Chrome is running "
            f"({found}) but none of them holds the LISTEN socket there — its DevTools "
            f"endpoint is dead and another unit's browser answers on this port"
        )
    if holder is not None:
        return DaemonProbe.port_taken(
            f"identity mismatch on CDP :{port}: the LISTEN socket belongs to pid "
            f"{holder}, which is not a Chrome on this cluster's profile {profile} — "
            f"another unit's browser holds this port"
        )
    return DaemonProbe.port_taken(
        f"identity mismatch on CDP :{port}: no Chrome running on this cluster's profile "
        f"{profile} is listening there — another unit's browser holds this port"
    )
