"""Stop a drained unit's recorded services without escalating to force.

The caller owns the maintenance journal and admission fence. These functions
prove only local recorded process identities; they do not prove remote drain or
stop OS-managed extras. Persistent terminals require a separate work boundary.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from cli.commands._maintenance_stop_report import (
    occupied_groups,
)
from shared.paths import run_dir
from shared.proc_tree import OwnedProcess, capture_tree
from shared.session_backend import get_shell_backend
from shared.session_record import SessionRecord
from shared.sessions.pty._paths import host_identity, host_starttime


def deadline_after(timeout: float) -> float:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("maintenance stop timeout must be finite and positive")
    return time.monotonic() + timeout


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("maintenance kept its hold; stop deadline expired")
    return value


def wait_for_exit(
    tracked: set[OwnedProcess],
    deadline: float,
    *,
    groups: tuple[int, ...] = (),
    escalate: Callable[[set[OwnedProcess]], None] | None = None,
) -> None:
    """Wait for `tracked` identities and `groups` to empty, within `deadline`.

    `escalate`, when given, is called once per iteration with the current
    living identities, AFTER the descendant re-capture: it may deliver further
    graceful signals to confirmed-owned identities (issue #2123 — a leader that
    exits without closing its children must not stall the stop until the
    deadline; the caller's escalator owns the ownership validation). Never
    escalates to SIGKILL and never certifies success while anything is alive.
    """
    while True:
        living = {identity for identity in tracked if identity.live()}
        occupied = occupied_groups(groups)
        if not living and not occupied:
            return
        for identity in living:
            tracked.update(capture_tree(identity))
        if escalate is not None:
            escalate(living)
        try:
            budget = remaining(deadline)
        except TimeoutError:
            raise TimeoutError(
                f"maintenance kept its hold; processes did not exit: "
                f"{sorted(identity.pid for identity in living)}; occupied process groups: {occupied}"
            ) from None
        time.sleep(min(0.05, budget))


def require_no_terminals() -> None:
    # A PTY host can remain alive after its shell exits. The ordinary listing
    # intentionally omits that retained record, so inspect both recorded births.
    terminals: list[str] = []
    for path in (run_dir() / "pty").glob("*.json"):
        record = SessionRecord.read(path)
        if record is None:
            raise RuntimeError(f"cannot verify terminal record: {path.stem}")
        identities = [OwnedProcess(record.pid, record.create_time, record.starttime)]
        host = host_identity(path)
        if host is not None:
            identities.append(OwnedProcess(host[0], host[1], host_starttime(path)))
        if any(identity.live() for identity in identities):
            terminals.append(path.stem)
    backend = get_shell_backend()
    listed = backend.list_sessions()
    terminals.extend(listed)
    if terminals:
        raise RuntimeError(
            "persistent terminals/schedules require their own completed-work boundary; "
            f"maintenance will not kill or replay them: {sorted(set(terminals))}"
        )


def stop_services(
    timeout: float, *, keep_terminals: bool = False, selected: frozenset[str] | None = None
) -> list[str]:
    """Ask the sole root owner to stop drained services without force escalation."""
    from cli.commands._root_driver import _root_tree_selection, _stop_root_service_tree

    deadline = deadline_after(timeout)
    if not keep_terminals:
        require_no_terminals()
    names = _root_tree_selection()
    selected_names = sorted(names if selected is None else names.keys() & selected)
    if selected is not None and not selected_names:
        return []
    preserve = frozenset(unit for name, unit in names.items() if name not in selected_names)
    _stop_root_service_tree(
        preserve=preserve,
        timeout_s=remaining(deadline),
        force=False,
        selected=None if selected is None else frozenset(names[name] for name in selected_names),
    )
    if not keep_terminals:
        require_no_terminals()
    return selected_names


def stop_data_plane(timeout: float, *, save: bool = True) -> list[str]:
    """Stop this home's native data plane; never stop a remote-managed plane."""
    from cli.commands._maintenance_data_plane import stop

    return stop(timeout, save=save)
