"""Orphan port-listener sweep for `ava stop` (Task #965 incident closure).

The named stop legs tear down what they can NAME (sessions, the native
session registry, the data plane, the registered extras); a service that
escaped its session — a gateway whose session died but whose uvicorn kept
the port, a pidfile daemon that outlived its stop, a gate process outside its
job — is invisible to every one of those legs and holds the cluster port
against the next start. This module is the port-level closure: every port the
unit expects to own is scanned, OUR listeners (the same repo/home ownership
predicate the start preflight applies) are killed verified, and foreign
listeners are never touched (the start's preflight reports them instead).
"""

from __future__ import annotations

from pathlib import Path

from cli.commands._repo import build_services
from shared.paths import ava_home


def _reap_orphan_listeners(
    repo: Path, home: Path, *, preserve: frozenset[str]
) -> list[tuple[str, int, int]]:
    """Sweep this unit's expected ports for OUR orphans and kill them verified.

    The stop legs above tear down what they can NAME (sessions, the native
    session registry, the data plane, the registered extras). A service that
    escaped its session — a gateway whose session died but whose uvicorn kept
    the port, a pidfile daemon that outlived its stop, a gate process outside
    its job — is invisible to every one of those legs, and it holds the cluster
    port against the next start: the new process dies on 'address already in
    use' while the old one keeps serving, which reads as the new code having
    taken effect when it never did (the 2026-08-07 pgbouncer / gate / gateway /
    events-maintenance incident class). This sweep is the port-level closure:
    for every port this unit expects to own, a listener whose pid mentions this
    unit's repo or home (the same ownership predicate the start preflight
    applies — shared.port_preflight.process_mentions) is by definition an
    orphan of a service this stop just killed, so it gets the verified
    terminate (SIGTERM + bounded wait + SIGKILL). A FOREIGN listener is never
    touched — it is another unit's or the operator's process, and the start's
    port preflight reports it instead.

    `preserve` names the services whose ports are legitimately still held:
    the browser (keep_browser — the login Chrome is preserved by design), the
    data plane (keep_infra — the update path keeps pg/redis/pgbouncer for the
    migrate step), and any session the caller explicitly preserved
    (cmd_update keeps the frontend serving across a backend-only bounce).

    Returns [(service, port, pid)] of reaped orphans for the stop output.
    """
    from cli.commands._pgbouncer import _terminate_verified
    from shared.port_preflight import listeners_on, process_mentions, unit_port_map

    markers = (str(repo.resolve()), str(home.resolve()))
    reaped: list[tuple[str, int, int]] = []
    for svc, port in sorted(unit_port_map(home).items()):
        if svc in preserve:
            continue
        for pid in listeners_on(port):
            if not process_mentions(pid, markers):
                continue  # foreign — never touch (the preflight reports it)
            if _terminate_verified(pid, label=f"orphan {svc} on port {port}"):
                reaped.append((svc, port, pid))
    return reaped


# The port key a preserved ServiceSpec session actually binds. The port table
# splits the UI across two slots (shared.port_block.PORT_OFFSETS): "frontend"
# is the ENTRY port, owned by the always-up gate (a converge-registered job
# with no ServiceSpec row — kept via `keep_gate` below), and "app" is the
# Next.js app the gate proxies to, bound by the `frontend` ServiceSpec. So
# preserving the `frontend` session protects the "app" slot — never the
# "frontend" slot, which belongs to the gate. Every other session's port key
# is its own name (gateway -> gateway, labeler -> labeler, ...).
_SESSION_PORT_KEYS: dict[str, str] = {"frontend": "app"}


def _reap_orphan_step(
    repo: Path,
    *,
    keep_browser: bool,
    keep_infra: bool,
    preserve_sessions: frozenset[str],
    keep_gate: bool,
) -> None:
    """Step 4 of `_do_stop`: sweep for orphan port listeners, report what died.

    Prints a summary line naming every reaped orphan; silent when nothing was
    left outside the sessions (the common case — the named legs above already
    tore the services down, and this step is the closure that turns the
    incident class into a no-op).

    `keep_gate` mirrors the stop's `teardown_extras`: an update / restart keeps
    the entry-port gate live by construction (teardown_extras=False), so its
    listener must never be swept as an orphan; a full stop
    (teardown_extras=True) tears the gate down via `stop_gate_service` and this
    sweep is what closes a gate that escaped that leg.

    Preserved sessions are matched by their BARE service name (`spec.session`,
    the same namespace `_compute_stop_scope` compares against) — not by the
    `ava-` prefixed session name. The two stop legs must speak one
    namespace: a session the stop leg preserved on a backend-only bounce
    (`cmd_update` passing `{"frontend"}`) was previously reaped here because
    this leg compared `session_name(spec.session)` == "ava-frontend" against
    the caller's bare "frontend" — the 2026-08-01-incident-class regression
    #1034 closes.
    """
    home = ava_home()
    preserve: set[str] = {"browser"} if keep_browser else set()
    if keep_infra:
        preserve.update({"postgres", "redis", "pgbouncer"})
    if keep_gate:
        # The entry port belongs to the always-up gate; a stop that keeps the
        # gate must not have its listener reaped as an orphan.
        preserve.add("frontend")
    # Sessions the caller explicitly preserved hold their ports legitimately
    # (cmd_update keeps `frontend` serving through a backend-only bounce).
    for spec in build_services():
        if spec.session in preserve_sessions:
            preserve.add(_SESSION_PORT_KEYS.get(spec.session, spec.session))

    print("\n→ sweep orphan port listeners")
    reaped = _reap_orphan_listeners(repo, home, preserve=frozenset(preserve))
    if not reaped:
        print("  (none)")
        return
    for svc, port, pid in reaped:
        print(f"  ✓ reaped orphan {svc} (port {port}, pid {pid})")
