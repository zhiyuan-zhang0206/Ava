"""`ava firewall` — the standalone face of the ALF allowlist manifest.

The converge step (`_converge_firewall.ensure_firewall_allowlist`) converges the
allowlist automatically on every `ava start` / `ava update`. These verbs give an
operator the same machinery on demand, without a full converge:

- `ava firewall status` — read-only: verdict, manifest coverage, stale rules,
  and whether the older-macOS sudo fallback grant is installed.
- `ava firewall sync` — run the rootless-first repair + prune pass now.

Mutation exits 0 as the ordinary user on macOS 15.3.1, but the daemon silently
drops an add whose bundle identifier already has a rule, so `sync` verifies
each rule by re-reading `--listapps` before reporting it. Other releases fall
back to `sudo -n`, then print manual commands if no non-interactive grant is
available.
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
    if audit.verdict is fw.FirewallVerdict.NOT_MACOS:
        print(f"  {audit.verdict.value}: {audit.detail}")
        return 0
    if audit.verdict not in (fw.FirewallVerdict.ALLOWED, fw.FirewallVerdict.RULES_MISSING):
        print(f"  {audit.verdict.value}: {audit.detail}")
    rules = fw.allowlisted_paths()
    if rules is None:
        print(f"  unreadable: could not read the allow list from {fw.SOCKETFILTERFW}")
        return 1
    print(fw.render_manifest_status(rules))
    stale = fw.stale_manifest_rules(rules)
    print(f"  stale rules: {len(stale)}")
    for path in stale:
        print(f"    - {path}")
    if fw.sudo_grant_installed():
        print("  sudo fallback grant: installed (used only if direct mutation is rejected)")
    else:
        print(
            "  sudo fallback grant: not installed (direct mutation was verified on "
            "the macmini running macOS 15.3.1; "
            "sync prints manual commands if this host requires elevation)"
        )
    return 0


def cmd_firewall_sync() -> int:
    """`ava firewall sync` — apply the manifest now (repair + prune).

    Mutates directly first. On older macOS it retries with `sudo -n`; if both
    paths fail, it prints the exact manual commands and exits 1.
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
    # Prune before adding so a stale rule's bundle identifier is free for the
    # replacement version's rule (macOS 15 ALF deduplicates by identifier).
    pruned = fw.prune_stale_rules(rules)
    if pruned.removed:
        refreshed = fw.allowlisted_paths()
        if refreshed is not None:
            rules = refreshed
    for path in pruned.removed:
        print(f"  · removed stale rule {path}")
    repair = fw.repair_allowlist(required, rules=rules)
    for path in repair.allowed:
        print(f"  · allowed {path}")
    if repair.failed:
        _report_missing(repair.failed, len(required))
        return 1
    if not repair.allowed and not pruned.removed:
        print("  already converged — nothing to do")
    return 0
