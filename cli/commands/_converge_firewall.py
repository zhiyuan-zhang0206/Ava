"""Converge's macOS Application Firewall check — converge the allowlist manifest.

The step has two moves. First, the diagnosis that has always been here: decide
*precisely* whether the host's serving binaries are allow-listed, and name the
defect when they are not. Second, the repair that used to be a printed pair of
`sudo` commands: attempt the same commands through `sudo -n`, which is silent
when the one-time NOPASSWD grant (scripts/install-firewall-sudoers.sh) is
installed and fails immediately when it is not.

A plain `sudo` would prompt for a password, and converge runs unattended from
`ava start`, the watchdog, and `ava cluster update`. A prompt in that path does
not degrade to "unfixed"; it hangs the bring-up, which is strictly worse than the
defect it was trying to repair, and on a headless host it hangs it invisibly.
`sudo -n` has none of that: it either runs the mutation or fails in a
millisecond, and a failure degrades to the historical behavior — print the exact
commands for the operator. See `shared.macos_firewall` for how the grant is
scoped and why this is safe to attempt at all.

Because it reports rather than enforces, it warns and returns instead of raising:
a missing firewall rule does not stop this host serving loopback, `ava start`
is how an operator recovers, and a converge step that blocked the recovery path
over something it cannot fix would be its own outage.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cli.commands._converge_spec import ConvergeCtx
from shared import macos_firewall as fw
from shared.macos_firewall import FirewallAudit, FirewallVerdict, audit_allowlist


def serving_binaries(roles: frozenset[str]) -> tuple[Path, ...]:
    """The binaries this unit serves a non-loopback port from, by capability.

    Resolved through the same helpers that *launch* them, because auditing a path
    the host does not actually run would prove nothing — and the paths are exactly
    where the fragility lives: both are version-stamped, so both orphan their ALF
    rule on a version bump. `sys.executable` is the version-stamped uv interpreter
    (`.../cpython-3.12.11-macos-aarch64-none/bin/python3.12`); `pg_tool` prefers
    the equally version-stamped vendored tree (`~/.ava/runtime/pg/17.4.0/bin`).

    The interpreter is audited for **either** capability, not just `gateway`. A
    gateway serves the HTTP port every runner dials (issue #949's symptom), and an
    agent-runner serves the ops port the *gateway* dials back — same interpreter,
    same ALF exposure, mirror-image outage. Postgres and redis are gateway-only:
    a runner holds no local data plane, so those binaries are absent there.

    Filtered to paths that exist, so a resolver falling through to a
    wrong-but-harmless default (`brew_prefix` does this when brew is absent)
    contributes a phantom "missing rule" instead of a real one.
    """
    from shared.paths import otel_collector_binary
    from shared.pg_tools import brew_prefix, pg_tool

    candidates = [Path(sys.executable)]
    if "gateway" in roles:
        candidates.append(pg_tool("postgres"))
        candidates.append(brew_prefix("redis") / "bin" / "redis-server")
        # A gateway with a cluster secret serves the collector's authenticated
        # remote OTLP receiver on AVA_MACHINE_HOST. Auditing it unconditionally
        # for gateway capability is harmless on a loopback-only single box (the
        # host-level audit exits before reading ALF there) and prevents a hybrid
        # gateway+runner from silently accepting only local telemetry.
        candidates.append(otel_collector_binary())
    return tuple(p for p in candidates if p.exists())


def audit_this_host(roles: frozenset[str]) -> FirewallAudit:
    """Audit this host's own serving binaries. The one entry point both callers share.

    Converge calls it to report proactively; the rollout's `OFF_BOX_UNREACHABLE`
    verdict calls it to explain a failure that already happened. Same detector, so
    the two can never disagree about whether the firewall is the cause.
    """
    from shared.machine import reachable_host

    required = serving_binaries(roles)
    if not required:
        return FirewallAudit(
            FirewallVerdict.ALLOWED, "no serving binary resolved on this host to audit"
        )
    return audit_allowlist(required, machine_host=reachable_host())


def ensure_firewall_allowlist(ctx: ConvergeCtx) -> None:
    """Converge this host's ALF allowlist to the declarative manifest.

    Order of work: diagnose (the audit decides whether this host can even have
    the defect — non-macOS, loopback-only and firewall-off hosts are no-ops),
    then repair every missing allow rule through `sudo -n`, then prune stale
    manifest-family rules. A healthy host sees nothing; a host that just got
    repaired sees one line per action; a host whose repair had no grant to use
    sees the exact commands, as before. `ctx.roles is None` (a fresh install,
    unit not yet configured) is treated as no capabilities and audits the
    interpreter alone.
    """
    audit = audit_this_host(frozenset(ctx.roles or ()))
    if audit.verdict is FirewallVerdict.UNREADABLE:
        # Not the defect, and not provably not-the-defect either. Say so once
        # rather than claiming a clean bill of health this step did not establish.
        print(f"  ! firewall: {audit.detail}", file=sys.stderr)
        return
    if audit.verdict not in (FirewallVerdict.ALLOWED, FirewallVerdict.RULES_MISSING):
        return  # not macOS / loopback-only / firewall off — nothing to converge

    rules = fw.allowlisted_paths()
    if rules is None:
        print(
            f"  ! firewall: could not read the allow list from {fw.SOCKETFILTERFW}",
            file=sys.stderr,
        )
        return

    # The audit's own missing set plus the full manifest: the audit resolves the
    # running interpreter through the launch helpers (catching a venv the globs
    # do not name), the manifest carries the whole declarative whitelist.
    required = {p.resolve() for p in audit.missing}
    required.update(fw.manifest_paths())
    required = tuple(sorted(required, key=str))

    repair = fw.repair_allowlist(required, rules=rules)
    pruned = fw.prune_stale_rules(rules)

    if repair.allowed:
        print(
            f"  · firewall: allowed {len(repair.allowed)} binaries (no popup on their next bind)",
            file=sys.stderr,
        )
    if pruned.removed:
        print(
            f"  · firewall: removed {len(pruned.removed)} stale allow rules",
            file=sys.stderr,
        )
    if repair.failed:
        _report_missing(repair.failed, len(required))


def _report_missing(missing: tuple[Path, ...], total: int) -> None:
    """The historical fallback: name the binaries and print the exact commands.

    Reached when the `sudo -n` repair had no grant to use (or a command failed).
    Keeps naming the OFF_BOX_UNREACHABLE verdict so both halves of the diagnosis
    still point at each other by name.
    """
    print(
        f"  ! firewall: {len(missing)} of {total} managed binaries have no ALF "
        "allow rule, so off-box connections to them are dropped while loopback "
        "still works",
        file=sys.stderr,
    )
    for path in missing:
        print(f"    - no allow rule: {path}", file=sys.stderr)
    print(
        "    left unfixed, this presents as OFF_BOX_UNREACHABLE on the next "
        "`ava cluster update` (the gateway serves loopback, no runner can reach it)",
        file=sys.stderr,
    )
    print(
        "    fix (needs root — converge tried via `sudo -n` but has no grant; "
        "install one once with `sudo scripts/install-firewall-sudoers.sh`, or "
        "paste the commands below):",
        file=sys.stderr,
    )
    for path in missing:
        for verb in ("--add", "--unblockapp"):
            print(f'      sudo "{fw.SOCKETFILTERFW}" {verb} "{path}"', file=sys.stderr)
    print(
        "    then restart the affected service so it re-binds under the new rule "
        "(an already-bound socket keeps the policy it was accepted under)",
        file=sys.stderr,
    )
