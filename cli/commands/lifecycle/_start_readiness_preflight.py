"""Read-only `ava start` state checks, run before a stop that a start must follow.

Restart checks the coming start's prerequisites while the current services still
serve. A local-state refusal leaves the running generation intact.

This module is the local-state half of "validate before kill": the read-only
parts of what start checks, moved in front of the stop, so a failure refuses
the stop while the host still serves.

What it forwards (each item read-only; nothing here repairs or launches):

- the `$AVA_HOME` private-tree skeleton — a `logs` / `workspaces` / `memory`
  root that converge would ABORT on (a symlink, or not a directory), and the
  `logs/.metadata_never_index` marker whose converge write requires a regular
  file. Non-regular nodes INSIDE the trees (sockets, FIFOs, devices) are
  reported as observations only: converge skips them, so start survives them;
- daemon health ports another home's daemon already answers on — the blocking
  pre-bind gate of `start._refuse_occupied_health_ports` (issue #977), which
  otherwise runs only after the stop;
- tracked migration files in the checked-out tree that cannot be read: the
  applier opens them inside start, and the pre-stop layout gate
  (`validate_migrations_at_ref`) vets names only;
- the two start prerequisites no other pre-stop gate covers: the
  prod-checkout anchoring rule, and the venv entry points — `.venv/bin/python`
  (what every service session launches through) always, `.venv/bin/ava` (what
  an external start process executes) only when
  the caller's start would exec it (`check_launcher`).

Contract: read-only, never raises for a finding — findings are data. Returns 0
to proceed with restart, 1 to refuse it. The caller answers a refusal with
RESTART_DECLINED ("nothing was stopped, host still serving"): unlike the
migrations-layout gate there is no revert, because the target tree is not at
fault — the host's local state is, and a retry re-checks it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from base.cluster.machine import MachineRoles
from base.host.private_storage import (
    private_file_problem,
    private_tree_root_problem,
    scan_non_regular_nodes,
)
from cli.start_runtime import StartRuntime

_TREE_ROOTS = ("logs", "workspaces", "memory")


def preflight_start_readiness(
    repo: Path, *, check_launcher: bool = True, runtime: StartRuntime | None = None
) -> int:
    """Vet the local state the coming `ava start` needs, before the stop.

    0 = proceed (any observations are printed); 1 = refuse, with every finding
    printed. See the module docstring for what is checked and why.

    `check_launcher=False` drops the `.venv/bin/ava` entry-point check for a
    caller whose start runs in-process and never execs it (`ava restart`);
    development's `.venv/bin/python` is still checked.
    """
    from base.paths import ava_home

    home = ava_home()
    if runtime is not None:
        runtime.validate()
    fatal: list[str] = []
    observations: list[str] = []

    checkout_problem = _prod_checkout_problem(repo)
    if checkout_problem is not None:
        fatal.append(checkout_problem)

    roles = _machine_roles()
    if roles is None:
        observations.append(
            "machine role did not resolve — port checks skipped (the probe gate "
            "ahead of this one reports the same condition)"
        )
    else:
        port_fatal, port_observations = _port_findings(roles)
        fatal += port_fatal
        observations += port_observations

    tree_fatal, tree_observations = _private_tree_findings(home)
    fatal += tree_fatal
    observations += tree_observations
    fatal += _migration_findings()
    fatal += _venv_findings(repo, check_launcher=check_launcher)

    return _report(fatal, observations)


def _prod_checkout_problem(repo: Path) -> str | None:
    """`ava start`'s first refusal — a home that carries its own `source` checkout
    may only launch from it (`base.paths.prod_service_checkout_error`) — moved
    ahead of the stop. Cheap and read-only; a home with no `source` of its own
    always passes it."""
    from base.paths import prod_service_checkout_error

    return prod_service_checkout_error(repo)


def _machine_roles() -> MachineRoles | None:
    """Resolve this host's roles via the canonical read-only accessor.

    `_roles_or_none` is the same helper `stop` / `status` / `converge/firewall_command.py` use
    (read the persisted capability set; None when the identity is not
    resolvable). None here means the caller skips the port checks: the probe
    gate that runs just before this one already refuses on that condition, so
    this gate does not have to fail twice for it.
    """
    import cli.commands._repo as _repo_commands

    return _repo_commands._roles_or_none()


def _port_findings(roles: MachineRoles) -> tuple[list[str], list[str]]:
    """(fatal, observations) for the health-port gate, checked on the roster that
    the update's own `ava start` will launch with.

    The fatal layer is the blocking pre-bind gate (#977): a daemon health port
    answered by another home's daemon refuses the whole start, and after the stop
    that refusal has no host left to serve. Only a *terminal* verdict counts — this
    unit's own daemons are ALIVE — so an idempotent restart still passes.

    Detection failures are observations, not refusals: a preflight must never be
    the thing that takes the host down.
    """
    import cli.commands.probe as _probe_commands
    import ops.roster as _roster
    from base.deploy.lifecycle.service_selection import resolve_selection
    from cli.commands.lifecycle.root_driver import _root_tree_roster

    try:
        available = {s.session for s in _roster.build_services()}
        roster = _root_tree_roster(roles, resolve_selection(available, persist=False))
        occupied = _probe_commands.occupied_health_ports(roster)
    except Exception as exc:  # a preflight must not fail the update
        return [], [f"health-port check skipped: {exc}"]

    fatal = [
        f"{port.spec.session}: health port answered by {port.detail} — `ava start` "
        "refuses the whole launch on this (#977); after the stop that refusal leaves "
        "no host serving. Stop the other home's daemon that holds the port"
        for port in occupied
    ]
    return fatal, []


def _private_tree_findings(home: Path) -> tuple[list[str], list[str]]:
    """(fatal, observations) for the private trees and skeleton converge owns.

    Fatal is what converge would raise on: a tree root that is a symlink or not
    a directory, the metadata marker being anything but a regular file, and
    `configs` / `secrets` existing as something mkdir cannot tolerate.
    Observations are the non-regular nodes inside the trees — converge skips
    them, so they cannot fail a start, but they are exactly the class that
    aborted the 2026-09-12 update after its stop, and seeing them before the
    stop is the point.
    """
    fatal: list[str] = []
    observations: list[str] = []

    for name in _TREE_ROOTS:
        root = home / name
        problem = private_tree_root_problem(root)
        if problem is not None:
            fatal.append(
                f"{root} {problem} — converge aborts the whole start on this root; "
                "fix the path (or move it aside) so a real directory can be created"
            )
        try:
            skipped = scan_non_regular_nodes(root)
        except OSError as exc:
            observations.append(f"could not scan {root} for non-regular files: {exc}")
            continue
        for node in skipped:
            observations.append(
                f"{node} is a socket/FIFO/device — converge skips it (no permission "
                "repair exists); remove it, or stop the process that owns it"
            )

    # `configs` / `secrets`: converge only mkdirs these (the same
    # `_ensure_ava_home_dirs` step), so a path that exists but is not a
    # directory makes that mkdir raise AFTER the stop — the same class as the
    # three tree roots above, under mkdir's own rule: a symlink TO a directory
    # is tolerated (`mkdir(exist_ok=True)`), a file or a dangling link is not.
    for name in ("configs", "secrets"):
        root = home / name
        if (root.is_symlink() or root.exists()) and not root.is_dir():
            fatal.append(
                f"{root} exists but is not a directory — converge's mkdir for it "
                "aborts the start; move it aside so a real directory can be created"
            )

    marker = home / "logs" / ".metadata_never_index"
    problem = private_file_problem(marker)
    if problem is not None:
        fatal.append(
            f"{marker} {problem} — converge's marker write refuses to run, aborting "
            "the start; replace it with a regular file"
        )
    return fatal, observations


def _migration_findings() -> list[str]:
    """Tracked migration files this checkout cannot read (the apply-side vet).

    `validate_migrations_at_ref` already vets the target's names before the
    stop; this asks the next question the applier will ask — can the file be
    opened — while the answer is still free.
    """
    from base.deploy.schema.migrations import MigrationLayoutError, unreadable_migration_files

    try:
        problems = unreadable_migration_files()
    except MigrationLayoutError as exc:
        return [f"migrations/ cannot be enumerated: {exc}"]
    return [
        f"migrations/{name} is not readable ({error}) — `ava start`'s apply would "
        "fail on the stopped host; fix its mode, or restore the file"
        for name, error in problems
    ]


def _venv_findings(repo: Path, *, check_launcher: bool) -> list[str]:
    """The venv entry points the coming start needs, keyed to who runs it.

    - `.venv/bin/python` — the interpreter every service session launches
      through (`ops/roster/__init__.py` builds `<venv>/bin/python -m ...`). Checked for
      every caller: `ava restart` has no `uv sync` verification behind it, so a
      damaged venv is exactly the "stopped and cannot come back" class this
      gate exists for.
    - `.venv/bin/ava` — the launcher the update leg's step 5 execs: a file that
      exists but cannot be executed makes its `subprocess.run` raise
      PermissionError (the OSError handler's "vanished (or is not executable)
      inside the stop window"). Step 3.5 vets presence; this adds the exec bit.
      Skipped where the start is in-process (`check_launcher=False`): an
      `ava restart` never execs it, and refusing a bounce over an entry point it
      does not use would block a viable restart.
    """
    from base.host.system.backend import get_backend

    backend = get_backend()
    findings = _entrypoint_findings(
        backend.venv_launcher("python", root=repo),
        label="the interpreter every service session launches through",
        fix="re-run `uv sync`",
    )
    if check_launcher:
        ava_bin = backend.venv_launcher("ava", root=repo)
        if ava_bin.is_file():  # a missing launcher is step 3.5's report
            findings += _entrypoint_findings(
                ava_bin,
                label="the launcher the update leg execs next",
                fix="`chmod +x` it, or re-run `uv sync`",
            )
    return findings


def _entrypoint_findings(binary: Path, *, label: str, fix: str) -> list[str]:
    """Why `binary` would fail the coming start, or [] when it is usable.

    Windows runs `.exe` files and has no exec bit to read; presence is the
    whole check there.
    """
    if not binary.is_file():
        return [f"{binary} is missing — {label} would fail after the stop; {fix}"]
    if os.name != "nt" and not os.access(binary, os.X_OK):
        return [f"{binary} is not executable — {label} would fail after the stop; {fix}"]
    return []


def _report(fatal: list[str], observations: list[str]) -> int:
    if observations:
        print("\n→ start-readiness observations (not blocking):")
        for line in observations:
            print(f"  ! {line}")
    if fatal:
        print(
            f"\n✗ start-readiness preflight: {len(fatal)} problem(s) would fail "
            "`ava start` only after the stop:",
            file=sys.stderr,
        )
        for line in fatal:
            print(f"    ✗ {line}", file=sys.stderr)
        return 1
    print("  ✓ start-readiness preflight passed")
    return 0
