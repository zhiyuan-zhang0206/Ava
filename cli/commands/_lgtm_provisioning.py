"""Converge rendering of the Grafana provisioning tree (task #3697).

Split out of ``_lgtm_native`` when the runtime-rendered dashboard (S3) pushed
that module over the per-file line ceiling. Every provisioning file is
copied VERBATIM under the content-hash user-edit guard — except
``dashboards/ava-ops-main.json``, which is generated from the metric
registries through ``shared.grafana_dashboard_supply``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from cli.commands._observatory_urls import _atomic_write
from cli.commands._rendered_file import write_rendered_guarded
from shared import telemetry

# The one provisioning file converge generates instead of copying verbatim
# (task #3697 S3): rendered from the metric registries, so plugin
# installs/uninstalls move its panels without hand edits.
_RENDERED_DASHBOARD_REL = "dashboards/ava-ops-main.json"


def _render_ava_ops_dashboard(dest: Path, hashes_path: Path, key: str) -> None:
    """Render the ava-ops dashboard into the native provisioning tree.

    Task #3697 slice S3: the dashboard's provisioning content is generated
    from the metric registries (core + plugin suppliers) instead of copied
    from the checkout, so plugin installs/uninstalls move its panels with no
    hand-edited JSON. Change-only: an identical render re-records the hash
    sidecar without touching the file (keeps converge idempotent and the
    Grafana restart fingerprint stable); a differing render goes through the
    same content-hash user-edit guard and atomic write as the verbatim
    copies. A render failure keeps the previous file and emits a warning
    event — a dashboard must never fail converge.
    """
    from shared.grafana_dashboard_supply import render_dashboard_json

    try:
        rendered, failed = render_dashboard_json()
    except Exception as exc:
        print(
            f"  ! lgtm native: ava-ops dashboard render failed ({exc}); keeping the previous file",
            file=sys.stderr,
        )
        telemetry.emit(
            "telemetry",
            "lgtm_dashboard_render_failed",
            level="warning",
            source="converge",
            attributes={"error": str(exc)[:300]},
        )
        return
    if failed:
        print(
            f"  ! lgtm native: plugin load failures (skipped, panels missing from this render): "
            f"{', '.join(failed)}",
            file=sys.stderr,
        )
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    if dest.exists() and dest.read_text(encoding="utf-8") == rendered:
        _record_rendered_hash(hashes_path, key, digest)
        return
    warning = write_rendered_guarded(dest, rendered, hashes_path, key, writer=_atomic_write)
    if warning is not None:
        print(f"  ! lgtm native: {warning}", file=sys.stderr)


def _record_rendered_hash(hashes_path: Path, key: str, digest: str) -> None:
    """Adopt ``digest`` for ``key`` in the hash sidecar without rewriting the
    tracked file (the identical-render path): a missing or stale record is
    filled in silently; an equal record is left untouched so the sidecar's
    bytes — and with them the Grafana restart fingerprint — stay put."""
    recorded: dict[str, str] = {}
    if hashes_path.exists():
        recorded = json.loads(hashes_path.read_text(encoding="utf-8"))
    if recorded.get(key) == digest:
        return
    recorded[key] = digest
    hashes_path.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")


def _render_provisioning(repo: Path, native_dir: Path) -> None:
    """Render the Grafana provisioning tree into the native config dir.

    Every provisioning file (datasources, alert rules, READMEs) is copied
    VERBATIM — except ``dashboards/ava-ops-main.json``, which is generated
    from the metric registries (task #3697 S3) — the datasource and webhook
    URLs are Grafana-native $__env{} references resolved from the process
    env (runtime.env), so the checkout file is always a valid configuration
    and the rendered copy never
    rewrites URLs. Each file is written atomically through the content-hash
    user-modification guard (web-sources precedent): a file the user
    hand-edited since the last converge write is warned about and preserved,
    never overwritten. Grafana reads this rendered tree via
    {{AVA_PROVISIONING_PATH}} — the deployment state is decoupled from the
    source checkout, so a rollout that swaps the checkout cannot feed the
    running instance a half-rendered tree.
    """
    source_dir = repo / "deploy/lgtm/config/grafana/provisioning"
    if not source_dir.is_dir():
        return
    dest_dir = native_dir / "config" / "provisioning"
    hashes_path = native_dir / "config" / "provisioning-hashes.json"
    rendered_relative: set[str] = set()
    for source in sorted(source_dir.rglob("*")):
        if not source.is_file():
            continue
        rel = source.relative_to(source_dir)
        if rel.as_posix() == _RENDERED_DASHBOARD_REL:
            # Generated from the metric registries right below (S3); the
            # checkout copy is the render's reference output, not the
            # provisioning source.
            continue
        rendered_relative.add(rel.as_posix())
        content = source.read_text(encoding="utf-8")
        warning = write_rendered_guarded(
            dest_dir / rel, content, hashes_path, rel.as_posix(), writer=_atomic_write
        )
        if warning is not None:
            print(f"  ! lgtm native: {warning}", file=sys.stderr)
    # The generated dashboard is never orphan-cleanup material: it has no
    # source template to vanish, so it stays in the rendered set even when
    # generation fails (the previous file must survive).
    rendered_relative.add(_RENDERED_DASHBOARD_REL)
    _render_ava_ops_dashboard(
        dest_dir / _RENDERED_DASHBOARD_REL, hashes_path, _RENDERED_DASHBOARD_REL
    )
    # Remove rendered files whose source template vanished (web-sources
    # _cleanup_gone_sources): untouched copies are pure derived state; a copy
    # the user edited is kept, loudly.
    for dest in sorted(dest_dir.rglob("*")):
        if not dest.is_file():
            continue
        rel = dest.relative_to(dest_dir).as_posix()
        if rel in rendered_relative:
            continue
        hashes: dict[str, str] = {}
        if hashes_path.exists():
            hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
        recorded = hashes.get(rel)
        if recorded is not None and hashlib.sha256(dest.read_bytes()).hexdigest() == recorded:
            dest.unlink()
            continue
        print(
            f"  ! lgtm native: rendered provisioning file {dest} has no source "
            "template anymore but was modified locally; kept",
            file=sys.stderr,
        )
