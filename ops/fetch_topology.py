"""Source-fetch topology policy for the rollout's Phase-0 fetch.

`settings.general.fetch_via_gateway` (cluster-pinned, default off) makes the
gateway the cluster's only wall-crossing fetcher. `ops.ops_cluster.cluster_fetch_op`
consults `fetch_wall_refusal()` before it fetches: a non-gateway host whose
`origin` still addresses a wall host (github.com) refuses — that refusal is what
keeps a rollout from silently falling back out of the central topology
(enable/rollback: `conventions/fetch-via-gateway-runbook.md`).

Kept out of `ops/ops_cluster.py`, whose line budget the rollout surface already
fills; this is the one concern here — where the cluster source fetch may point.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from shared.config import settings
from shared.gitenv import git_env
from shared.machine import is_gateway
from shared.proc import run_bounded

# Local read of one config line; the same bound class as ops_cluster's resolve reads.
_ORIGIN_READ_TIMEOUT_S = 5.0


def git_origin_url(repo_dir: Path) -> str:
    """The configured `origin` fetch URL of the source checkout (`""` when unreadable)."""
    resolve = run_bounded(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        env=git_env(),
        timeout=_ORIGIN_READ_TIMEOUT_S,
    )
    return resolve.stdout.strip() if resolve.returncode == 0 else ""


def fetch_wall_refusal(origin_url: str) -> str | None:
    """The refusal message when the central-fetch topology must refuse a fetch from
    `origin_url`, else None.

    Refuses only when all three hold: the switch is on, this host does not carry
    the gateway capability, and the origin addresses a wall host. Fetching GitHub
    from a runner under the switch would silently fall back out of the topology —
    the refusal makes that fallback loud (the rollout aborts before any pause).
    """
    if not settings.general.fetch_via_gateway or is_gateway() or not _wall_source(origin_url):
        return None
    return (
        "fetch_via_gateway is on but this host's origin still addresses a wall "
        f"host ({origin_url or 'origin unreadable'}); point origin at the gateway "
        "source (conventions/fetch-via-gateway-runbook.md) — refusing to fetch "
        "GitHub from a non-gateway host"
    )


def _wall_source(url: str) -> bool:
    """True when a fetch from `url` would cross the wall (a GitHub host). The host
    is parsed first, so a URL merely mentioning github.com in its path does not count."""
    host = _host_of(url)
    return host == "github.com" or host.endswith(".github.com")


def _host_of(url: str) -> str:
    """The host a git fetch URL addresses — `ssh://`/`https://` forms and the
    scp-like `[user@]host:path` form. Empty when unparseable."""
    url = url.strip()
    if not url:
        return ""
    if "://" in url:
        return (urlsplit(url).hostname or "").lower()
    scp = re.match(r"(?:[^@/:]+@)?([^:/]+):", url)
    return scp.group(1).lower() if scp else ""
