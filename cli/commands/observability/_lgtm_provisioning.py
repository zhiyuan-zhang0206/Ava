"""Converge rendering of the Grafana provisioning tree (task #3697).

Split out of ``observability.lgtm_native`` when the runtime-rendered dashboard (S3) pushed
that module over the per-file line ceiling. Every provisioning file is
copied VERBATIM under the content-hash user-edit guard — except
``dashboards/meiva-ops-main.json``, which is generated from the metric
registries through ``base.packages.dashboard_supply``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx

from base import telemetry
from base.agents.context.clients import DatabaseFactory
from base.telemetry import EventPipeline
from cli.commands.converge.rendered_file import write_rendered_guarded

from .observatory_urls import atomic_write

# The one provisioning file converge generates instead of copying verbatim
# (task #3697 S3): rendered from the metric registries, so plugin
# installs/uninstalls move its panels without hand edits.
_DASHBOARD_UID = "meiva-ops-main"
RENDERED_DASHBOARD_REL = f"dashboards/{_DASHBOARD_UID}.json"
_DASHBOARD_SOURCE_KEY = "$dashboard-source"
DASHBOARD_FOLDER = "Meiva"


# meiva:link cli/commands/observability/docs/lgtm/dashboard-provisioning.meiva.okf.md
def _render_meiva_ops_dashboard(
    dest: Path,
    hashes_path: Path,
    key: str,
    reference: Path,
    *,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
) -> None:
    from base.packages.dashboard_supply import render_dashboard_json

    try:
        rendered, failed = render_dashboard_json(database_factory())
    except Exception as exc:
        print(
            f"  ! lgtm native: meiva-ops dashboard render failed ({exc})",
            file=sys.stderr,
        )
        _install_dashboard_fallback(
            dest, hashes_path, key, reference, error=str(exc)[:300], producer=producer
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
        record_rendered_hash(hashes_path, key, digest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(dest, rendered)
    record_rendered_hash(hashes_path, key, digest)
    print(f"  lgtm native: meiva-ops dashboard re-rendered (sha256 {digest[:12]})")


# meiva:link cli/commands/observability/docs/lgtm/dashboard-provisioning.meiva.okf.md
def _install_dashboard_fallback(
    dest: Path,
    hashes_path: Path,
    key: str,
    reference: Path,
    *,
    error: str,
    producer: Callable[[], EventPipeline],
) -> None:
    outcome = "kept"
    if dashboard_uid(dest) == _DASHBOARD_UID and dashboard_panels(dest):
        print(
            f"  ! lgtm native: keeping the installed dashboard at {dest}",
            file=sys.stderr,
        )
    else:
        content = _usable_reference(reference, error=error, producer=producer)
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(dest, content)
        record_rendered_hash(
            hashes_path,
            key,
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source="reference",
        )
        outcome = "reference"
        print(
            f"  ! lgtm native: no usable installed dashboard; "
            f"installed the checkout reference {reference}",
            file=sys.stderr,
        )
    telemetry.emit(
        "telemetry",
        "lgtm_dashboard_render_failed",
        producer=producer,
        level="warning",
        source="converge",
        attributes={"error": error, "outcome": outcome},
    )


def _usable_reference(reference: Path, *, error: str, producer: Callable[[], EventPipeline]) -> str:
    content = reference.read_text(encoding="utf-8") if reference.is_file() else ""
    if content and dashboard_uid(reference) == _DASHBOARD_UID and dashboard_panels(reference):
        return content
    telemetry.emit(
        "telemetry",
        "lgtm_dashboard_render_failed",
        producer=producer,
        level="error",
        source="converge",
        attributes={"error": error, "outcome": "failed"},
    )
    raise RuntimeError(
        "meiva-ops dashboard render failed, the installed file is not this dashboard, and "
        f"the checkout reference {reference} is missing or unusable: refusing to converge "
        "with no dashboards"
    )


def dashboard_document(path: Path) -> dict[str, Any]:
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast("dict[str, Any]", document) if isinstance(document, dict) else {}


def dashboard_uid(path: Path) -> str:
    uid = dashboard_document(path).get("uid")
    return uid if isinstance(uid, str) else ""


def dashboard_panels(path: Path) -> list[Any]:
    panels = dashboard_document(path).get("panels")
    return cast("list[Any]", panels) if isinstance(panels, list) else []


# meiva:link cli/commands/observability/docs/lgtm/dashboard-provisioning.meiva.okf.md
def record_rendered_hash(
    hashes_path: Path, key: str, digest: str, *, source: str = "render"
) -> None:
    recorded: dict[str, Any] = {}
    if hashes_path.exists():
        recorded = json.loads(hashes_path.read_text(encoding="utf-8"))
    if recorded.get(key) == digest and recorded.get(_DASHBOARD_SOURCE_KEY) == source:
        return
    recorded[key] = digest
    recorded[_DASHBOARD_SOURCE_KEY] = source
    hashes_path.parent.mkdir(parents=True, exist_ok=True)
    hashes_path.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")


def render_provisioning(
    repo: Path,
    native_dir: Path,
    *,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
) -> None:
    """Render the Grafana provisioning tree into the native config dir.

    Every provisioning file (datasources, alert rules, READMEs) is copied
    VERBATIM — except ``dashboards/meiva-ops-main.json``, which is generated
    from the metric registries (task #3697 S3) — the datasource and webhook
    URLs are Grafana-native $__env{} references resolved from the process
    env (runtime.env), so the checkout file is always a valid configuration
    and the rendered copy never
    rewrites URLs. Each file is written atomically through the content-hash
    user-modification guard (web-sources precedent): a file the user
    hand-edited since the last converge write is warned about and preserved,
    never overwritten. Grafana reads this rendered tree via
    {{MEIVA_PROVISIONING_PATH}} — the deployment state is decoupled from the
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
        if rel.as_posix() == RENDERED_DASHBOARD_REL:
            # Generated from the metric registries right below (S3); the
            # checkout copy is the render's reference output, not the
            # provisioning source.
            continue
        rendered_relative.add(rel.as_posix())
        content = source.read_text(encoding="utf-8")
        warning = write_rendered_guarded(
            dest_dir / rel,
            content,
            hashes_path,
            rel.as_posix(),
            surface="lgtm-provisioning",
            writer=atomic_write,
        )
        if warning is not None:
            print(f"  ! lgtm native: {warning}", file=sys.stderr)
    # The generated dashboard is never orphan-cleanup material: it has no
    # source template to vanish, so it stays in the rendered set even when
    # generation fails (the previous file must survive).
    rendered_relative.add(RENDERED_DASHBOARD_REL)
    _render_meiva_ops_dashboard(
        dest_dir / RENDERED_DASHBOARD_REL,
        hashes_path,
        RENDERED_DASHBOARD_REL,
        source_dir / RENDERED_DASHBOARD_REL,
        database_factory=database_factory,
        producer=producer,
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
    _prune_ghost_hashes(dest_dir, hashes_path)


# meiva:link cli/commands/observability/docs/lgtm/dashboard-provisioning.meiva.okf.md
def _prune_ghost_hashes(dest_dir: Path, hashes_path: Path) -> None:
    recorded = _recorded_hashes(hashes_path)
    ghosts = sorted(
        key for key in recorded if not key.startswith("$") and not (dest_dir / key).is_file()
    )
    if not ghosts:
        return
    for key in ghosts:
        del recorded[key]
    hashes_path.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
    print(f"  lgtm native: dropped hash entries with no rendered file: {', '.join(ghosts)}")


def _recorded_hashes(hashes_path: Path) -> dict[str, Any]:
    try:
        recorded: object = json.loads(hashes_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast("dict[str, Any]", recorded) if isinstance(recorded, dict) else {}


def _object(value: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _objects(value: Any) -> list[dict[str, Any]]:
    items = cast("list[Any]", value) if isinstance(value, list) else []
    return [cast("dict[str, Any]", item) for item in items if isinstance(item, dict)]


def _pre_rename_folder(folder: dict[str, Any]) -> bool:
    return any(str(folder.get(field) or "").lower().startswith("ava") for field in ("uid", "title"))


def _grafana_get(url: str) -> tuple[int, Any]:
    try:
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(2.0)) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        return 0, f"{type(exc).__name__}: {exc}"
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def _dashboard_file_problems(dashboard_path: Path) -> list[str]:
    if not dashboard_document(dashboard_path):
        return [f"dashboard file is missing or not a JSON object: {dashboard_path}"]
    problems: list[str] = []
    uid = dashboard_uid(dashboard_path)
    if uid != _DASHBOARD_UID:
        problems.append(f"dashboard uid is {uid!r}, expected {_DASHBOARD_UID!r}")
    if not dashboard_panels(dashboard_path):
        problems.append("dashboard carries no panels")
    return problems


def _sidecar_problems(hashes_path: Path) -> list[str]:
    recorded = _recorded_hashes(hashes_path)
    problems: list[str] = []
    if RENDERED_DASHBOARD_REL not in recorded:
        problems.append(f"{hashes_path.name} does not record {RENDERED_DASHBOARD_REL}")
    if recorded.get(_DASHBOARD_SOURCE_KEY) == "reference":
        problems.append("the installed dashboard is the checkout reference, not a render")
    problems.extend(
        f"{hashes_path.name} still records the pre-rename {key}"
        for key in sorted(recorded)
        if key.startswith("dashboards/ava-")
    )
    return problems


def _grafana_problems(fetch: Callable[[str], tuple[int, Any]], grafana_url: str) -> list[str]:
    status, payload = fetch(f"{grafana_url}/api/dashboards/uid/{_DASHBOARD_UID}")
    if status == 0:
        return [f"Grafana is unreachable at {grafana_url}: {payload}"]
    problems: list[str] = []
    if status != 200:
        problems.append(f"Grafana answered {status} for dashboard uid {_DASHBOARD_UID!r}")
    else:
        folder = _object(_object(payload).get("meta")).get("folderTitle")
        if folder != DASHBOARD_FOLDER:
            problems.append(f"dashboard sits in folder {folder!r}, expected {DASHBOARD_FOLDER!r}")
    status, payload = fetch(f"{grafana_url}/api/folders")
    if status != 200:
        problems.append(f"Grafana answered {status} for /api/folders")
        return problems
    problems.extend(
        f"pre-rename folder still present: {folder.get('title')!r} ({folder.get('uid')})"
        for folder in _objects(payload)
        if _pre_rename_folder(folder)
    )
    return problems


# meiva:link cli/commands/observability/docs/lgtm/dashboard-provisioning.meiva.okf.md
def verify_dashboard_provisioning(
    native_dir: Path,
    *,
    grafana_url: str,
    get: Callable[[str], tuple[int, Any]] | None = None,
) -> list[str]:
    config_dir = native_dir / "config"
    return [
        *_dashboard_file_problems(config_dir / "provisioning" / RENDERED_DASHBOARD_REL),
        *_sidecar_problems(config_dir / "provisioning-hashes.json"),
        *_grafana_problems(get or _grafana_get, grafana_url),
    ]
