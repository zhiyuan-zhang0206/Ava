"""`ava lgtm render` — the ava-ops dashboard render, from the metric registries.

Task #3697 slice S1 (parent #3689): ``shared.grafana_dashboard`` renders the
dashboard from the registered ``MetricSpec`` set and
``shared.grafana_dashboard_supply`` collects the plugin side (checkout plugins
plus enabled installed ones). This command is the operator surface for that
render: it diffs the render against THIS host's native Grafana provisioning
copy (the live ``ava-ops-main.json``) or writes the render with ``--force``.
Slice S3 reuses the same render path inside converge, where the render
replaces the checkout copy as the provisioning source.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

__all__ = ["cmd_grafana_render"]

_DASHBOARD_FILE = "ava-ops-main.json"


def _provisioning_dashboard(home: Path) -> Path:
    """The native provisioning copy converge renders into (S3 target)."""
    return home / "lgtm" / "native" / "config" / "provisioning" / "dashboards" / _DASHBOARD_FILE


def _atomic_write(path: Path, content: str) -> None:
    """Write via a sibling temp file + replace — a reader never sees a partial
    file, and a failed write leaves the previous file untouched."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _panels_by_identity(content: str) -> dict[str, Any] | None:
    """Panel id+title -> panel JSON for one dashboard file; None when the
    content is not parseable as a dashboard (reported, not crashed on)."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    return {f"{panel.get('id')}:{panel.get('title')}": panel for panel in data.get("panels", [])}


def _report_diff(target: Path, current: str, rendered: str) -> None:
    """Print what the render would change about the current file."""
    have = _panels_by_identity(current)
    want = _panels_by_identity(rendered)
    if have is None or want is None:
        print(
            f"{target} is not a readable dashboard JSON — the render replaces it wholesale "
            f"({len(want) if want else '?'} panels)",
            file=sys.stderr,
        )
        return
    added = sorted(want.keys() - have.keys())
    removed = sorted(have.keys() - want.keys())
    changed = sorted(key for key in want.keys() & have.keys() if want[key] != have[key])
    print(
        f"render differs: {len(added)} added, {len(removed)} removed, {len(changed)} changed",
        file=sys.stderr,
    )
    for label, keys in (("added", added), ("removed", removed), ("changed", changed)):
        if keys:
            printed = ", ".join(keys[:8]) + (" …" if len(keys) > 8 else "")
            print(f"  {label}: {printed}", file=sys.stderr)


def cmd_grafana_render(*, force: bool, repo_only: bool) -> int:
    """Render the ava-ops dashboard and diff it (default) or write it (--force).

    The spec set is the checkout's plugins plus, unless ``--repo_only``, the
    enabled installed-plugin registry rows — the same dual supplier slice S3
    uses in converge. Exit code: 0 when the provisioning copy already matches
    (or was written), 1 when it differs (diff preview) or the render cannot
    run on this host.
    """
    from shared.paths import ava_home

    target = _provisioning_dashboard(ava_home())
    if not target.parent.is_dir():
        print(
            f"no native Grafana provisioning tree at {target.parent} — this host does not "
            "run the LGTM observability stack (`ava lgtm on` or converge installs it)",
            file=sys.stderr,
        )
        return 1

    from shared.grafana_dashboard_supply import render_dashboard_json

    rendered, failed = render_dashboard_json(repo_only=repo_only)
    if failed:
        print(
            f"plugin load failures (skipped, panels missing from this render): {', '.join(failed)}",
            file=sys.stderr,
        )
    current = target.read_text(encoding="utf-8") if target.is_file() else None

    if current == rendered:
        print(
            f"{target.name}: in sync ({len(json.loads(rendered)['panels'])} entries, sha256 {_digest(rendered)[:12]})"
        )
        return 0

    if not force:
        if current is None:
            print(f"{target} is absent — the render would create it", file=sys.stderr)
        else:
            _report_diff(target, current, rendered)
        print("run `ava lgtm render --force` to write the render", file=sys.stderr)
        return 1

    _atomic_write(target, rendered)
    print(
        f"{target}: written ({len(json.loads(rendered)['panels'])} entries, sha256 {_digest(rendered)[:12]})"
    )
    return 0
