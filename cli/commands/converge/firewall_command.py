"""`meiva firewall` — the standalone face of the ALF allowlist manifest.

The converge step (`firewall.ensure_firewall_allowlist`) converges the
allowlist automatically on every source `meiva start`. These verbs give an
operator the same machinery on demand, without a full converge:

- `meiva firewall status` — read-only: verdict, manifest coverage, stale rules,
  and whether the older-macOS sudo fallback grant is installed.
- `meiva firewall sync` — run the rootless-first repair + prune pass now.

Mutation exits 0 as the ordinary user on macOS 15.3.1, but the daemon silently
drops an add whose bundle identifier already has a rule, so `sync` verifies
each rule by re-reading `--listapps` before reporting it. Other releases fall
back to `sudo -n`, then print manual commands if no non-interactive grant is
available.
"""

from __future__ import annotations

import sys

from base.cluster.machine import MachineIdentity
from base.host.system import macos_firewall as fw
from base.host.system.hostport import Host
from cli.commands.converge.firewall import (
    audit_this_host,
    configured_machine_name,
    report_missing,
)


def cmd_firewall_status(*, machine: MachineIdentity, host: Host) -> int:
    """`meiva firewall status` — audit the host and diff the manifest (read-only)."""
    from cli.commands import _repo

    roles = _repo.roles_or_none(machine=machine)
    audit = audit_this_host(frozenset(roles or ()), machine=machine, host=host)
    print("→ firewall status")
    if audit.verdict is fw.FirewallVerdict.NOT_MACOS:
        print(f"  {audit.verdict.value}: {audit.detail}")
        return 0
    if audit.verdict not in (fw.FirewallVerdict.ALLOWED, fw.FirewallVerdict.RULES_MISSING):
        print(f"  {audit.verdict.value}: {audit.detail}")
    rules = fw.allowlisted_paths(host=host)
    if rules is None:
        print(f"  unreadable: could not read the allow list from {fw.SOCKETFILTERFW}")
        return 1
    print(
        fw.render_manifest_status(rules, machine_name=configured_machine_name(machine), host=host)
    )
    stale = fw.stale_manifest_rules(rules)
    print(f"  stale rules: {len(stale)}")
    for path in stale:
        print(f"    - {path}")
    if fw.sudo_grant_installed(host=host):
        print("  sudo fallback grant: installed (used only if direct mutation is rejected)")
    else:
        print(
            "  sudo fallback grant: not installed (direct mutation was verified on "
            "the macmini running macOS 15.3.1; "
            "sync prints manual commands if this host requires elevation)"
        )
    return 0


def cmd_firewall_sync(*, machine: MachineIdentity, host: Host) -> int:
    """`meiva firewall sync` — apply the manifest now (repair + prune).

    Mutates directly first. On older macOS it retries with `sudo -n`; if both
    paths fail, it prints the exact manual commands and exits 1.
    """
    from cli.commands import _repo

    roles = _repo.roles_or_none(machine=machine)
    audit = audit_this_host(frozenset(roles or ()), machine=machine, host=host)
    if audit.verdict not in (fw.FirewallVerdict.ALLOWED, fw.FirewallVerdict.RULES_MISSING):
        print(f"→ firewall sync: nothing to do ({audit.detail})")
        return 0
    rules = fw.allowlisted_paths(host=host)
    if rules is None:
        print(
            f"  ! firewall: could not read the allow list from {fw.SOCKETFILTERFW}", file=sys.stderr
        )
        return 1
    required = {p.resolve() for p in audit.missing}
    required.update(fw.manifest_paths(machine_name=configured_machine_name(machine), host=host))
    required = tuple(sorted(required, key=str))
    # Prune before adding so a stale rule's bundle identifier is free for the
    # replacement version's rule (macOS 15 ALF deduplicates by identifier).
    pruned = fw.prune_stale_rules(rules, host=host)
    if pruned.removed:
        refreshed = fw.allowlisted_paths(host=host)
        if refreshed is not None:
            rules = refreshed
    for path in pruned.removed:
        print(f"  · removed stale rule {path}")
    repair = fw.repair_allowlist(required, rules=rules, host=host)
    for path in repair.allowed:
        print(f"  · allowed {path}")
    if repair.failed:
        report_missing(repair.failed, len(required))
        return 1
    if not repair.allowed and not pruned.removed:
        print("  already converged — nothing to do")
    return 0
