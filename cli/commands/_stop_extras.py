"""Full-stop extras for `ava stop` — processes converge registered OUTSIDE the
service session roster.

`_do_stop`'s service loop + agent reap cover everything the platform supervisor
started as a ServiceSpec; but converge also registers two things under the OS
supervisor, which that loop never sees and a plain kill does not stick on
(launchd KeepAlive respawns):

- the fleet UI gate (`services.gate`): a launchd KeepAlive LaunchAgent on
  macOS, a user-systemd unit on Linux, and a detached process on other POSIX systems;
- the permissions-helper LaunchAgent (`com.ava.permissions-helper.<slug>`).

These were observed surviving a "full" `ava stop` on the preview teardown
2026-08-06 (gate still on :18001 with a stale run/gate.pid; the helper respawned
with a fresh pid right after SIGTERM). Only a full stop tears them down —
`cmd_update` / `cmd_restart` pass teardown_extras=False so the gate keeps the
entry port live through a rollout by construction.
"""

from __future__ import annotations

import sys


def stop_gate_service() -> None:
    """Stop the fleet UI gate on a full `ava stop` (see module docstring)."""
    from cli.commands._converge_gate import (
        _bootout_and_wait,
        _job_loaded,
        gate_label,
        gate_pid_is_ours,
    )
    from cli.commands._pgbouncer import _terminate_verified
    from shared.paths import ava_home, repo_root
    from shared.platform import IS_MACOS

    home = ava_home()
    if IS_MACOS:
        label = gate_label(home)
        if not _job_loaded(label):
            return
        if _bootout_and_wait(label):
            print("  ✓ gate launchd job booted out")
        else:
            print(
                f"  ⚠ gate launchd job {label} still loaded after bootout — "
                "boot it out manually before `ava start`",
                file=sys.stderr,
            )
        return
    if sys.platform == "linux":
        from cli.commands._gate_systemd import stop, unit_path
        from shared.machine import is_gateway

        # A pure runner has no gate to supervise. A registration/pidfile still
        # requires cleanup even if the current capability has changed.
        if (
            not unit_path(home).exists()
            and not (home / "run/gate.pid").exists()
            and not is_gateway()
        ):
            return
        if stop(home):
            print("  ✓ gate systemd user unit stopped")
    # Legacy Linux and other POSIX gateways: a detached process with a pidfile. Verify the pid
    # is this checkout's gate daemon before signalling it — a recycled number
    # would otherwise take a SIGTERM and then a SIGKILL.
    #
    # Deliberately NOT `gate_pid`: that one clears a pidfile it rejects, which is
    # right at the launch site (converge knows the repo that owns the home) and
    # wrong here. `ava cluster down --path` runs this against ANOTHER home while
    # `repo_root()` is still the invoking checkout, so "not ours" here can mean
    # "that home's gate, launched by its own checkout" — leave its pidfile intact
    # and say so rather than stranding a live daemon nothing can find. The next
    # converge on the owning checkout clears a genuinely stale file.
    pidfile = home / "run" / "gate.pid"
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        pidfile.unlink(missing_ok=True)
        return
    if not gate_pid_is_ours(pid, repo_root()):
        print(
            f"  · gate daemon not stopped: {pidfile} names pid {pid}, which this "
            f"checkout cannot confirm as its own gate — not signalling it",
            file=sys.stderr,
        )
        return
    _terminate_verified(pid, label="gate daemon")
    pidfile.unlink(missing_ok=True)


def stop_permissions_helper() -> None:
    """Boot out this cluster's permissions-helper LaunchAgent on a full stop.

    The bundle id is fixed across clusters so one TCC grant covers all; the
    label is per-cluster home slug.
    """
    from cli.commands._converge_gate import _bootout_and_wait, _job_loaded
    from shared.cluster import home_slug
    from shared.paths import ava_home
    from shared.platform import IS_MACOS

    if not IS_MACOS:
        return
    label = f"com.ava.permissions-helper.{home_slug(ava_home())}"
    if not _job_loaded(label):
        return
    if _bootout_and_wait(label):
        print("  ✓ permissions-helper launchd job booted out")
    else:
        print(
            f"  ⚠ permissions-helper launchd job {label} still loaded after "
            "bootout — boot it out manually before `ava start`",
            file=sys.stderr,
        )
