"""`ava packages` — the per-machine package refresh surface.

`status` is read-only: it joins the install registry (schema v2) with the
derived host version and each package's declared manifest range, so the
"which content is on this machine, from where, under what policy, and what did
the last check do" question has one place to look (design §5.6; tasks #2915 /
#3267).

The executor verbs (`refresh`, `rollback`, `policy`) land with the P1 skills
fast lane and extend this module.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from shared.install_registry import InstalledPackage

# Column width for the text table's LAST RESULT cell (JSON is untruncated).
_RESULT_W = 40


@dataclass(frozen=True)
class _Row:
    """One package's status line — the text table and the JSON share it."""

    name: str
    kind: str
    origin: str
    enabled: bool
    channel: str | None
    mode: str
    interval_seconds: int | None
    applied_rev: str | None
    last_check_at: str | None
    last_apply_at: str | None
    last_result: str | None
    next_check_at: str | None
    manifest: dict[str, object] | None
    manifest_error: str | None


def _read_declared_range(pkg: InstalledPackage) -> tuple[dict[str, object] | None, str | None]:
    """(manifest dict | None, error | None) for a package's on-disk manifest.

    Reads the LIVE copy on this machine (what would actually load), not the
    source tree: a skill package carries its optional manifest beside SKILL.md
    (`ava-plugin.json`, the same format/validator as plugins). A manifest that
    fails validation is reported as an error value instead of raising — status
    must never fail on the state it reports.
    """
    from shared import paths, plugin_manifest

    root = {
        "skill": paths.skills_dir() / pkg.name,
        "plugin": paths.plugins_dir() / pkg.name,
        "mcp": paths.mcps_dir() / pkg.name,
    }.get(pkg.type)
    if root is None or not root.is_dir():
        return None, None
    try:
        manifest = plugin_manifest.load_manifest(root)
    except plugin_manifest.ManifestError as e:
        return None, str(e)
    if manifest is None:
        return None, None
    return {
        "version": manifest.version,
        "engines": manifest.engines,
        "requires_commit": manifest.requires_commit,
    }, None


def _next_check_at(last_check_at: str | None, interval_seconds: int | None) -> str | None:
    """ISO timestamp of the next due check; None = due now / no interval."""
    if interval_seconds is None or last_check_at is None:
        return None
    try:
        last = datetime.fromisoformat(last_check_at)
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (last + timedelta(seconds=interval_seconds)).isoformat(timespec="seconds")


def _fmt_due(next_check_at: str | None) -> str:
    if next_check_at is None:
        return "due"
    try:
        due = datetime.fromisoformat(next_check_at)
    except ValueError:
        return "due"
    remaining = due - datetime.now(UTC)
    if remaining.total_seconds() <= 0:
        return "due"
    hours, rest = divmod(int(remaining.total_seconds()), 3600)
    minutes = rest // 60
    return f"in {hours}h{minutes:02d}m" if hours else f"in {minutes}m"


def _fmt_range(manifest: dict[str, object] | None, manifest_error: str | None) -> str:
    if manifest_error is not None:
        return "invalid"
    if manifest is None:
        return "-"
    engines = manifest["engines"]
    parts: list[str] = []
    if isinstance(engines, dict):
        typed_engines = cast("dict[str, str]", engines)
        parts = [f"{host} {rng}" for host, rng in typed_engines.items()]
    commit = manifest.get("requires_commit")
    if isinstance(commit, str) and commit:
        parts.append(f"commit {commit[:7]}")
    return ", ".join(parts) if parts else "-"


def cmd_packages_status(*, json_output: bool = False) -> int:
    """`ava packages status [--json]` — host version, channels, and per-package
    channel/policy/applied-rev/last-result/declared-range. Read-only."""
    import json

    from shared import host_version as host_version_mod
    from shared import install_registry, paths

    registry = install_registry.load()
    try:
        host_bare: str | None = host_version_mod.host_version()
        host_display: str | None = host_version_mod.host_version_display()
    except host_version_mod.HostVersionError:
        host_bare = None
        host_display = "unknown"

    rows: list[_Row] = []
    for pkg in sorted(registry.packages, key=lambda p: p.name):
        policy = install_registry.resolved_policy(pkg)
        manifest, manifest_error = _read_declared_range(pkg)
        rows.append(
            _Row(
                name=pkg.name,
                kind=pkg.type,
                origin=pkg.origin,
                enabled=pkg.enabled,
                channel=policy.channel,
                mode=policy.mode,
                interval_seconds=policy.interval_seconds,
                applied_rev=pkg.update.applied_rev,
                last_check_at=pkg.update.last_check_at,
                last_apply_at=pkg.update.last_apply_at,
                last_result=pkg.update.last_result,
                next_check_at=_next_check_at(pkg.update.last_check_at, policy.interval_seconds),
                manifest=manifest,
                manifest_error=manifest_error,
            )
        )

    if json_output:
        payload = {
            "host": {
                "version": host_bare,
                "display": host_display,
                "home": str(paths.ava_home()),
                "registry_schema": registry.version,
            },
            "channels": {
                name: {
                    "remote_url": ch.remote_url,
                    "ref": ch.ref,
                    "last_seen_sha": ch.last_seen_sha,
                    "last_checked_at": ch.last_checked_at,
                    "last_result": ch.last_result,
                }
                for name, ch in sorted(registry.channels.items())
            },
            "packages": [
                {
                    "name": r.name,
                    "kind": r.kind,
                    "origin": r.origin,
                    "enabled": r.enabled,
                    "channel": r.channel,
                    "mode": r.mode,
                    "interval_seconds": r.interval_seconds,
                    "applied_rev": r.applied_rev,
                    "last_check_at": r.last_check_at,
                    "last_apply_at": r.last_apply_at,
                    "last_result": r.last_result,
                    "next_check_at": r.next_check_at,
                    "manifest": r.manifest,
                    "manifest_error": r.manifest_error,
                }
                for r in rows
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(
        f"[ava packages status] host {host_display} · home {paths.ava_home()} "
        f"· registry v{registry.version}"
    )
    if registry.channels:
        for name, ch in sorted(registry.channels.items()):
            head = ch.last_seen_sha[:7] if ch.last_seen_sha else "-"
            suffix = f"  ({ch.last_result})" if ch.last_result else ""
            print(
                f"  channel {name}@{ch.ref}  {ch.remote_url}  head {head}  "
                f"last check {ch.last_checked_at or 'never'}{suffix}"
            )
    else:
        print("  channel core: not fetched yet")
    if not rows:
        print("  (no tracked packages)")
        return 0
    print(
        f"  {'NAME':<30} {'KIND':<6} {'ORIGIN':<7} {'CHANNEL':<8} {'MODE':<7} "
        f"{'INTERVAL':<9} {'APPLIED':<9} {'LAST RESULT':<{_RESULT_W}} "
        f"{'NEXT CHECK':<10} RANGE"
    )
    for r in rows:
        applied = (r.applied_rev or "-")[:8]
        last_result = r.last_result or "-"
        if len(last_result) > _RESULT_W:
            last_result = last_result[: _RESULT_W - 3] + "..."
        interval_text = "-" if r.interval_seconds is None else f"{r.interval_seconds // 3600}h"
        print(
            f"  {r.name:<30} {r.kind:<6} {r.origin:<7} "
            f"{(r.channel or '-'):<8} {r.mode:<7} {interval_text:<9} "
            f"{applied:<9} {last_result:<{_RESULT_W}} "
            f"{_fmt_due(r.next_check_at):<10} {_fmt_range(r.manifest, r.manifest_error)}"
        )
    if host_bare is None:
        print(
            "  ! host version unknown (not a git checkout and no pyproject version)",
            file=sys.stderr,
        )
    return 0
