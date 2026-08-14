"""`ava firewall` — the standalone face of the ALF allowlist manifest.

The converge step (`_converge_firewall.ensure_firewall_allowlist`) converges the
allowlist automatically on every `ava start` / `ava update`. These verbs give an
operator the same machinery on demand, without a full converge:

- `ava firewall status` — read-only: verdict, manifest coverage, stale rules,
  and whether the one-time sudoers grant is installed.
- `ava firewall sync` — run the repair + prune pass now (needs the grant).

The one-time grant itself is installed by `scripts/install-firewall-sudoers.sh`,
run once per host with `sudo` (see `shared.macos_firewall` for why).
"""

from __future__ import annotations

import sys

from cli.commands._converge_firewall import _report_missing, audit_this_host
from shared import macos_firewall as fw


def cmd_firewall_status() -> int:
    """`ava firewall status` — audit the host and diff the manifest (read-only)."""
    import cli.commands as _ns

    roles = _ns._roles_or_none()
    audit = audit_this_host(frozenset(roles or ()))
    print("→ firewall status")
    if audit.verdict not in (fw.FirewallVerdict.ALLOWED, fw.FirewallVerdict.RULES_MISSING):
        print(f"  {audit.verdict.value}: {audit.detail}")
        return 0
    rules = fw.allowlisted_paths()
    if rules is None:
        print(f"  unreadable: could not read the allow list from {fw.SOCKETFILTERFW}")
        return 1
    required = fw.manifest_paths()
    missing = fw.missing_allow_rules(required, rules)
    stale = fw.stale_manifest_rules(rules)
    print(f"  manifest: {len(required) - len(missing)} of {len(required)} binaries allow-listed")
    for path in missing:
        print(f"    - no allow rule: {path}")
    print(f"  stale rules: {len(stale)}")
    for path in stale:
        print(f"    - {path}")
    if fw.sudo_grant_installed():
        print("  sudoers grant: installed — converge repairs automatically")
    else:
        print(
            "  sudoers grant: NOT installed — run once: "
            "`sudo scripts/install-firewall-sudoers.sh`, then `ava firewall sync`"
        )
    return 0


def cmd_firewall_sync() -> int:
    """`ava firewall sync` — apply the manifest now (repair + prune).

    Needs the sudoers grant; without it, prints the exact manual commands and
    exits 1.
    """
    import cli.commands as _ns

    roles = _ns._roles_or_none()
    audit = audit_this_host(frozenset(roles or ()))
    if audit.verdict not in (fw.FirewallVerdict.ALLOWED, fw.FirewallVerdict.RULES_MISSING):
        print(f"→ firewall sync: nothing to do ({audit.detail})")
        return 0
    rules = fw.allowlisted_paths()
    if rules is None:
        print(
            f"  ! firewall: could not read the allow list from {fw.SOCKETFILTERFW}", file=sys.stderr
        )
        return 1
    required = {p.resolve() for p in audit.missing}
    required.update(fw.manifest_paths())
    required = tuple(sorted(required, key=str))
    repair = fw.repair_allowlist(required, rules=rules)
    pruned = fw.prune_stale_rules(rules)
    for path in repair.allowed:
        print(f"  · allowed {path}")
    for path in pruned.removed:
        print(f"  · removed stale rule {path}")
    if repair.failed:
        _report_missing(repair.failed, len(required))
        return 1
    if not repair.allowed and not pruned.removed:
        print("  already converged — nothing to do")
    return 0
