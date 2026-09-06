"""Full-stop extras outside the service roster: Gate, helper and native LGTM.

Only this home's registrations are stopped. Definitions, observability data and
LGTM enablement are retained so a normal start can restore the same services.
Updates and pause preserve these extras. Failures propagate to the stop caller.
"""

from __future__ import annotations

import sys

from cli.commands._stop_supervised import stop_detached, stop_launchd, stop_systemd


def stop_gate_service(*, force: bool = False, timeout_s: float = 30.0) -> None:
    """Stop this home's fleet UI gate, without changing its desired state."""
    from cli.commands._converge_gate import gate_label, gate_pid_is_ours
    from shared.paths import ava_home, repo_root
    from shared.platform import IS_MACOS

    home = ava_home()
    if IS_MACOS:
        stop_launchd(gate_label(home), force=force, timeout_s=timeout_s)
        return
    if sys.platform == "linux":
        from cli.commands._gate_systemd import unit_name, unit_path
        from shared.machine import is_gateway

        if unit_path(home).exists() or (home / "run/gate.pid").exists() or is_gateway():
            stop_systemd(
                unit_name(home), force=force, timeout_s=timeout_s, expected_fragment=unit_path(home)
            )
    # A legacy detached daemon can survive its systemd replacement. Its pidfile
    # may belong to another checkout, so verify ownership before any signal and
    # preserve the evidence on refusal or incomplete stop.
    pidfile = home / "run/gate.pid"
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        pidfile.unlink(missing_ok=True)
        return
    if not gate_pid_is_ours(pid, repo_root()):
        from shared.proc import process_alive

        if process_alive(pid):
            raise RuntimeError(
                f"gate daemon not stopped: {pidfile} names pid {pid}, which this "
                "checkout cannot confirm as its own gate; not signalling it"
            )
        pidfile.unlink(missing_ok=True)
        return
    stop_detached(pid, force=force, timeout_s=timeout_s)
    pidfile.unlink(missing_ok=True)


def stop_permissions_helper(*, force: bool = False, timeout_s: float = 30.0) -> None:
    """Stop this home's macOS helper; the Windows helper is user-wide."""
    from shared.cluster import home_slug
    from shared.paths import ava_home
    from shared.platform import IS_MACOS

    if IS_MACOS:
        stop_launchd(
            f"com.ava.permissions-helper.{home_slug(ava_home())}",
            force=force,
            timeout_s=timeout_s,
        )


def stop_lgtm_services(*, force: bool = False, timeout_s: float = 30.0) -> None:
    """Stop native backends without removing the station marker or their data."""
    from cli.commands._maintenance_stop import deadline_after, remaining
    from shared.lgtm_local import BACKENDS
    from shared.paths import ava_home
    from shared.platform import IS_MACOS

    home = ava_home()
    deadline = deadline_after(timeout_s)
    # Consumers stop before the stores. The gateway/watchdog service roster must
    # already be stopped, otherwise it could reconverge the retained marker.
    for name in reversed(BACKENDS):
        if IS_MACOS:
            from cli.commands._lgtm_native import native_label

            stop_launchd(native_label(name, home), force=force, timeout_s=remaining(deadline))
        elif sys.platform == "linux":
            from shared.lgtm_systemd import unit_name, unit_path

            # Pure runners need no user-manager connection for absent LGTM.
            if not unit_path(home, name).exists() and not (home / "lgtm/native").exists():
                continue
            stop_systemd(
                unit_name(home, name),
                force=force,
                timeout_s=remaining(deadline),
                expected_fragment=unit_path(home, name),
            )
