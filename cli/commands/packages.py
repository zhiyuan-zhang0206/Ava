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
from pathlib import Path
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
    host_blocked: str | None = None


def _package_root(pkg: InstalledPackage) -> Path | None:
    """The package's LIVE directory on this machine (None when absent)."""
    from shared import paths

    root = {
        "skill": paths.skills_dir() / pkg.name,
        "plugin": paths.plugins_dir() / pkg.name,
        "mcp": paths.mcps_dir() / pkg.name,
    }.get(pkg.type)
    if root is None or not root.is_dir():
        return None
    return root


def _read_declared_range(pkg: InstalledPackage) -> tuple[dict[str, object] | None, str | None]:
    """(manifest dict | None, error | None) for a package's on-disk manifest.

    Reads the LIVE copy on this machine (what would actually load), not the
    source tree: a skill package carries its optional manifest beside SKILL.md
    (`ava-plugin.json`, the same format/validator as plugins). A manifest that
    fails validation is reported as an error value instead of raising — status
    must never fail on the state it reports.
    """
    from shared import plugin_manifest

    root = _package_root(pkg)
    if root is None:
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
        root = _package_root(pkg)
        host_blocked = install_registry.host_contract_reason(root) if root is not None else None
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
                host_blocked=host_blocked,
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
                    "host_blocked": r.host_blocked,
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
    for r in rows:
        if r.host_blocked is not None:
            print(f"  ! {r.name}: not loadable on this host — {r.host_blocked}")
    if host_bare is None:
        print(
            "  ! host version unknown (not a git checkout and no pyproject version)",
            file=sys.stderr,
        )
    return 0


def cmd_packages_refresh(
    *,
    check_only: bool = False,
    only: str | None = None,
    json_output: bool = False,
    force: bool = False,
    from_job: bool = False,
) -> int:
    """`ava packages refresh [--check] [--package NAME] [--force] [--json]
    [--from-job]` — check the channels and apply due updates to this machine's
    skill packages. The same code path serves the OS job (`--from-job`); manual
    runs work regardless of cadence. Returns 1 only when the registry is
    unreadable (a pass-level failure), not for per-package outcomes — those are
    recorded in the registry and shown here."""
    import json

    from cli.commands._packages_refresh import run_refresh

    report = run_refresh(check_only=check_only, only=only, force=force, from_job=from_job)
    if json_output:
        payload = {
            "ran": report.ran,
            "skip_reason": report.skip_reason,
            "channel": report.channel_line,
            "items": [
                {
                    "name": item.name,
                    "kind": item.kind,
                    "channel": item.channel,
                    "mode": item.mode,
                    "result": item.result,
                }
                for item in report.items
            ],
            "counts": report.counts,
            "notes": list(report.notes),
        }
        print(json.dumps(payload, indent=2))
        return 0
    if not report.ran:
        print(f"[ava packages refresh] skipped: {report.skip_reason}")
        return 1 if (report.skip_reason or "").startswith("registry unreadable") else 0
    suffix = " (check only)" if check_only else ""
    head = f" · {report.channel_line}" if report.channel_line else ""
    print(f"[ava packages refresh]{suffix}{head}")
    for item in report.items:
        print(f"  {item.kind:<6} {item.name:<30} {item.channel:<5} {item.mode:<7} {item.result}")
    for note in report.notes:
        print(f"  note: {note}")
    summary = ", ".join(f"{key} {value}" for key, value in sorted(report.counts.items()))
    print(f"  summary: {summary or '(nothing to do)'}")
    return 0


def _swap_trees_with_prev(skills: Path, name: str, skip: frozenset[tuple[str, ...]]) -> Path:
    """Swap `skills/<name>` with `skills/.<name>.prev`, carrying marker-protected
    local subtrees from the current tree across into the restored one. Returns
    the restored destination path."""
    import shutil

    dest = skills / name
    prev = skills / f".{name}.prev"
    stash = skills / f".{name}.keep"
    if stash.exists():
        shutil.rmtree(stash)
    for parts in skip:
        sub = dest.joinpath(*parts)
        if sub.exists():
            moved_to = stash.joinpath(*parts)
            moved_to.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sub), str(moved_to))
    swap = skills / f".{name}.rollback"
    if swap.exists():
        shutil.rmtree(swap)
    if dest.exists():
        shutil.move(str(dest), str(swap))
    shutil.move(str(prev), str(dest))
    if swap.exists():
        shutil.move(str(swap), str(prev))
    for parts in skip:
        sub = stash.joinpath(*parts)
        if sub.exists():
            moved_to = dest.joinpath(*parts)
            moved_to.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sub), str(moved_to))
    if stash.exists():
        shutil.rmtree(stash)
    return dest


def cmd_packages_rollback(name: str, *, force: bool = False) -> int:
    """`ava packages rollback <name> [--force]` — restore the package's previous
    tree (the `.<name>.prev` kept by the last successful apply), swapping it with
    the current one. `--force` overrides the local-edit guard. The channel
    watermark (`applied_rev`) is left where it was: a later refresh applies only
    what changed after the revoked rev."""
    from shared import install_registry, paths
    from shared.skill_names import match_key

    try:
        registry = install_registry.load()
    except install_registry.InstallRegistryError as exc:
        print(f"[ava packages rollback] registry unreadable: {exc}", file=sys.stderr)
        return 1
    row = next((p for p in registry.packages if match_key(p.name) == match_key(name)), None)
    if row is None:
        print(f"[ava packages rollback] no tracked package named '{name}'", file=sys.stderr)
        return 1
    if row.type != "skill":
        print(
            f"[ava packages rollback] '{row.name}' is a {row.type} package; P1 rollback covers skills only",
            file=sys.stderr,
        )
        return 1
    skills = paths.skills_dir()
    dest = skills / row.name
    prev = skills / f".{row.name}.prev"
    if not prev.is_dir():
        print(
            f"[ava packages rollback] no previous tree at {prev} (nothing applied yet?)",
            file=sys.stderr,
        )
        return 1
    skip = install_registry.preserved_subpaths(dest)
    recorded = row.installed_hash or row.content_hash
    if (
        not force
        and dest.exists()
        and install_registry.copy_changed(dest, recorded, skip_subtrees=skip)
    ):
        print(
            "[ava packages rollback] current copy differs from the last applied content "
            "(local edits); re-run with --force to replace it",
            file=sys.stderr,
        )
        return 1
    dest = _swap_trees_with_prev(skills, row.name, skip)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with install_registry.mutate() as reg:
        fresh = next((p for p in reg.packages if match_key(p.name) == match_key(row.name)), None)
        if fresh is not None:
            fresh.content_hash = install_registry.tree_hash(dest, skip_subtrees=skip)
            fresh.updated_at = stamp
            fresh.update.last_apply_at = stamp
            fresh.update.last_result = "rolled_back: previous tree restored"
    print(f"[ava packages rollback] '{row.name}': previous tree restored -> {dest}")
    print("  active on the next skill scan.")
    return 0


def cmd_packages_policy(
    name: str, *, update_mode: str | None = None, check_every: str | None = None
) -> int:
    """`ava packages policy <name> [--update-mode auto|notify|off]
    [--check-every 24h]` — record an explicit policy decision on the row; explicit
    values survive every refresh pass (only None fields are resolved from
    settings)."""
    from cli.commands._packages_refresh import parse_duration
    from shared import install_registry
    from shared.skill_names import match_key

    if update_mode is None and check_every is None:
        print("[ava packages policy] pass --update-mode and/or --check-every", file=sys.stderr)
        return 1
    if update_mode is not None and update_mode not in ("auto", "notify", "off"):
        print(
            f"[ava packages policy] unknown update mode {update_mode!r} (auto | notify | off)",
            file=sys.stderr,
        )
        return 1
    try:
        interval = parse_duration(check_every) if check_every else None
    except ValueError as exc:
        print(f"[ava packages policy] {exc}", file=sys.stderr)
        return 1
    try:
        registry = install_registry.load()
    except install_registry.InstallRegistryError as exc:
        print(f"[ava packages policy] registry unreadable: {exc}", file=sys.stderr)
        return 1
    row = next((p for p in registry.packages if match_key(p.name) == match_key(name)), None)
    if row is None:
        print(f"[ava packages policy] no tracked package named '{name}'", file=sys.stderr)
        return 1
    with install_registry.mutate() as reg:
        fresh = next((p for p in reg.packages if match_key(p.name) == match_key(row.name)), None)
        if fresh is None:
            print(f"[ava packages policy] '{row.name}' vanished from the registry", file=sys.stderr)
            return 1
        if update_mode is not None:
            fresh.update.mode = update_mode
        if interval is not None:
            fresh.update.interval_seconds = interval
    updated = install_registry.get(row.name)
    if updated is not None:
        policy = install_registry.resolved_policy(updated)
        note = "" if policy.channel is not None else "  (no channel — refresh won't act on it)"
        if policy.mode == "off":
            note = "  (off — checks and applies are skipped)"
        print(
            f"[ava packages policy] '{updated.name}': mode={policy.mode} "
            f"interval={policy.interval_seconds}s channel={policy.channel or '-'}{note}"
        )
    return 0
