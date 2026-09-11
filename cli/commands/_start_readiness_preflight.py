"""Read-only `ava start` state checks, run before a stop that a start must follow.

Two callers, one rule — a check that can only fail AFTER the stop fails on a
host whose services are already down (the 2026-09-12 incident shape, where a
stray workspace socket aborted converge an hour after the stop took the
fleet's coordinator offline on macmini):

- the self-update leg (`_update_agent_runner` step 3.1) — `ava start` is step 5
  there, so every local check it makes lands after the stop;
- `ava restart` (`cli/commands/stop.py`, task #3165) — the same stop→start
  shape on the operator verb and, on Windows, on the updater ladder's restart
  step. Every category this gate refuses on would also fail that restart's own
  start leg, so a refusal never blocks a viable bounce — the listed repairs are
  the path, and a refusal leaves the host exactly as it was.

This module is the local-state half of "validate before kill": the read-only
parts of what start checks, moved in front of the stop, so a failure refuses
the stop while the host still serves.

What it forwards (each item read-only; nothing here repairs or launches):

- the `$AVA_HOME` private-tree skeleton — a `logs` / `workspaces` / `memory`
  root that converge would ABORT on (a symlink, or not a directory), and the
  `logs/.metadata_never_index` marker whose converge write requires a regular
  file. Non-regular nodes INSIDE the trees (sockets, FIFOs, devices) are
  reported as observations only: converge skips them, so start survives them;
- daemon health ports another unit already answers on — the blocking pre-bind
  gate of `start._refuse_occupied_health_ports` (issue #977), which otherwise
  runs only after the stop. Its warning-only sibling — the full port block plus
  `.env`/registry drift (issue #603) — rides along as observations;
- tracked migration files in the checked-out tree that cannot be read: the
  applier opens them inside start, and the pre-stop layout gate
  (`validate_migrations_at_ref`) vets names only;
- the two start prerequisites no other pre-stop gate covers: the
  prod-checkout anchoring rule, and the venv entry points — `.venv/bin/python`
  (what every service session launches through) always, `.venv/bin/ava` (what
  the update leg's step 5 execs; its presence is step 3.5's report) only when
  the caller's start would exec it (`check_launcher`).

Contract: read-only, never raises for a finding — findings are data. Returns 0
to proceed with the update, 1 to refuse it. The caller answers a refusal with
RESTART_DECLINED ("nothing was stopped, host still serving"): unlike the
migrations-layout gate there is no revert, because the target tree is not at
fault — the host's local state is, and a retry re-checks it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from shared.machine import MachineRoles
from shared.private_storage import (
    private_file_problem,
    private_tree_root_problem,
    scan_non_regular_nodes,
)

_TREE_ROOTS = ("logs", "workspaces", "memory")


def preflight_start_readiness(repo: Path, *, check_launcher: bool = True) -> int:
    """Vet the local state the coming `ava start` needs, before the stop.

    0 = proceed (any observations are printed); 1 = refuse, with every finding
    printed. See the module docstring for what is checked and why.

    `check_launcher=False` drops the `.venv/bin/ava` entry-point check for a
    caller whose start runs in-process and never execs it (`ava restart`);
    `.venv/bin/python` — what every service session DOES launch through — is
    checked for every caller.
    """
    from shared.paths import ava_home

    home = ava_home()
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
        port_fatal, port_observations = _port_findings(repo, home, roles)
        fatal += port_fatal
        observations += port_observations

    tree_fatal, tree_observations = _private_tree_findings(home)
    fatal += tree_fatal
    observations += tree_observations

    fatal += _migration_findings()
    fatal += _venv_findings(repo, check_launcher=check_launcher)

    return _report(fatal, observations)


def _prod_checkout_problem(repo: Path) -> str | None:
    """`ava start`'s first refusal — the prod home may only launch from its own
    anchored checkout (`shared.paths.prod_service_checkout_error`) — moved ahead
    of the stop. Cheap and read-only; a non-prod unit always passes it."""
    from shared.paths import prod_service_checkout_error

    return prod_service_checkout_error(repo)


def _machine_roles() -> MachineRoles | None:
    """Resolve this host's roles via the canonical read-only accessor.

    `_roles_or_none` is the same helper `stop` / `status` / `_firewall` use
    (read the persisted capability set; None when the identity is not
    resolvable). None here means the caller skips the port checks: the probe
    gate that runs just before this one already refuses on that condition, so
    this gate does not have to fail twice for it.
    """
    import cli.commands as _ns

    return _ns._roles_or_none()


def _port_findings(repo: Path, home: Path, roles: MachineRoles) -> tuple[list[str], list[str]]:
    """(fatal, observations) for the two port layers, checked on the roster that
    the update's own `ava start` will launch with.

    The fatal layer is the blocking pre-bind gate (#977): a daemon health port
    answered by another unit refuses the whole start, and after the stop that
    refusal has no host left to serve. Only a *terminal* verdict counts — this
    unit's own daemons are ALIVE — so an idempotent restart still passes. The
    observation layer is the warning-only scan (#603): the full port block plus
    `.env`/registry drift, surfaced here while the operator is watching the
    updater instead of only inside the post-stop converge.

    Detection failures are observations, not refusals, mirroring
    `_port_preflight.ensure_port_preflight`'s contract: a preflight must never
    be the thing that takes the host down.
    """
    import cli.commands as _ns
    from cli.commands._converge_spec import ConvergeCtx
    from cli.commands._port_preflight import collect_port_conflicts
    from shared import cluster
    from shared.disabled_services import resolve_launch_skip
    from shared.port_preflight import env_port_drift

    try:
        roster = _ns._launch_roster(roles, resolve_launch_skip(set(), persist=False))
        occupied = _ns._occupied_health_ports(roster)
    except Exception as exc:  # a preflight must not fail the update
        return [], [f"health-port check skipped: {exc}"]

    fatal = [
        f"{port.spec.session}: health port answered by {port.detail} — `ava start` "
        "refuses the whole launch on this (#977); after the stop that refusal leaves "
        "no host serving. Free the port, or move this unit's block: `ava enroll "
        "--gateway <url> --machine-name <name> --machine-host <host> "
        "--health-port-base <N>`"
        for port in occupied
    ]

    try:
        ctx = ConvergeCtx(repo=repo, ava_home=home, roles=roles)
        observations = list(collect_port_conflicts(ctx))
        record = cluster.get_record(home)
        if record is not None:
            observations += env_port_drift(home, record)
    except Exception as exc:  # same contract as the outer guard
        observations = [f"port-block scan skipped: {exc}"]
    return fatal, observations


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
    from shared.migrations import MigrationLayoutError, unreadable_migration_files

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
      through (`ops/roster.py` builds `<venv>/bin/python -m ...`). Checked for
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
    from shared.platform_backend import get_backend

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
