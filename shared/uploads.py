"""Shared helpers for agent file uploads.

An uploaded file for agent N lives at ``~/Downloads/AvaAgent-N/<name>`` and is
addressed over HTTP as ``/api/agents/N/uploads/<name>``. Three call sites must
agree on that mapping: the gateway upload endpoint (writes the file + serves it
back), the gateway message endpoint (validates that an image referenced in a
multimodal message really is one of this agent's uploads), and the agent claim
node (reads image uploads to inline them as native model content). Centralizing
the path <-> url mapping keeps those three in lockstep.

Path resolution is traversal-safe: `resolve_upload_path` refuses any name that
escapes the agent's upload directory, so a crafted url / filename cannot reach
an arbitrary file.
"""

from __future__ import annotations

import base64
from pathlib import Path

from shared.private_storage import ensure_private_dir

# Suffix -> MIME for image uploads that can be inlined as native model content.
# Mirrors the image row of `ava._understand`'s media map; a suffix not listed
# here is not treated as an inlineable image (the message endpoint 422s a
# reference to it, the claim node never base64-inlines it).
IMAGE_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def agent_upload_dir(agent_id: int) -> Path:
    """Directory holding agent `agent_id`'s uploaded files."""
    return ensure_private_dir(Path.home() / "Downloads" / f"AvaAgent-{agent_id}")


def sanitize_upload_name(filename: str) -> str:
    """Strip directory separators from an uploaded filename (no traversal on write)."""
    return filename.replace("/", "_").replace("\\", "_")


def upload_url(agent_id: int, name: str) -> str:
    """The HTTP path an uploaded file is addressed by."""
    return f"/api/agents/{agent_id}/uploads/{name}"


def parse_upload_url(url: str) -> tuple[int, str] | None:
    """Split an upload url into (agent_id, name), or None if it is not the
    ``/api/agents/<int>/uploads/<name>`` shape. Does not check the file exists
    or that `name` is safe — callers pair this with `resolve_upload_path`."""
    prefix = "/api/agents/"
    if not url.startswith(prefix):
        return None
    rest = url[len(prefix) :]
    agent_part, sep, name = rest.partition("/uploads/")
    if not sep or not name or not agent_part.isdigit():
        return None
    return int(agent_part), name


def resolve_upload_path(agent_id: int, name: str) -> Path:
    """Resolve an upload name to its on-disk path, guarding against traversal.

    Raises:
        ValueError: `name` escapes the agent's upload directory (``..`` / an
            absolute path / a separator that survived).
    """
    base = agent_upload_dir(agent_id).resolve()
    target = (base / name).resolve()
    if target == base or not target.is_relative_to(base):
        raise ValueError(f"upload name {name!r} escapes agent {agent_id} upload dir")
    return target


def image_mime_for(name: str) -> str | None:
    """MIME type for an image upload name, or None if the suffix is not an image."""
    return IMAGE_MIME.get(Path(name).suffix.lower())


def fetch_upload_b64(agent_id: int, name: str) -> tuple[str, str]:
    """Fetch an uploaded image from the gateway over HTTP and return
    (mime_type, base64_ascii).

    The upload physically lives on the gateway (the frontend's single entry
    point writes it there); its address is the `/api/agents/<id>/uploads/<name>`
    URL. Fetching that URL is the one uniform path for every agent — a remote
    runner's claim node must not read the gateway's local disk (the same-host
    assumption class as the inspector's local session probe). The fetch carries
    the cluster-secret bearer, same as every other agent -> gateway dial.

    Raises:
        ValueError: `name` is not a recognized image type.
        OSError: the upload is gone or the gateway is unreachable (the claim
            node degrades the image block to a text note on this).
    """
    mime = image_mime_for(name)
    if mime is None:
        raise ValueError(f"upload {name!r} is not a recognized image type")
    import httpx

    from shared.cluster_auth import bearer_header
    from shared.config import settings
    from shared.http_dial import get as http_get
    from shared.machine import gateway_api_base

    url = f"{gateway_api_base().rstrip('/')}/api/agents/{agent_id}/uploads/{name}"
    secret = settings.data_plane.cluster_secret
    try:
        resp = http_get(url, headers=bearer_header(secret) if secret else {}, timeout=15.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise OSError(f"fetch upload {url!r} failed: {exc}") from exc
    return mime, base64.standard_b64encode(resp.content).decode("ascii")


# ── upload limits (audit round-2 security P1-2) ─────────────────────────────
# The upload endpoint is the gateway's one authenticated user->disk write
# surface. Without caps it is an unlimited write-to-disk DoS (any caller with
# a session/Bearer can fill the disk) and its serve-back path can hand a
# browser a rendered HTML/SVG payload (stored XSS). These constants bound the
# write; `serve_upload` headers bound the serve-back. Deliberately constants,
# not config: a limit that can be tuned away per-cluster is a limit that
# silently disappears; operators who need more change the constant.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # per file — read-into-memory + disk cap
MAX_AGENT_UPLOAD_BYTES = 500 * 1024 * 1024  # total per agent (quota)
MAX_AGENT_UPLOAD_FILES = 1000  # file count per agent (inode-ish bound)

# Suffixes whose content executes in a browser when served inline (direct
# navigation / iframe). Served with Content-Disposition: attachment +
# X-Content-Type-Options: nosniff instead of refused, so a legitimate upload
# (an HTML mockup an agent is asked to analyze) still works — the agent reads
# files itself and is unaffected by how the browser would render them.
_RENDERABLE_SUFFIXES = frozenset({".html", ".htm", ".svg", ".xhtml", ".xml", ".md"})


def upload_quota_used(agent_dir: Path) -> tuple[int, int]:
    """(total bytes, file count) of everything currently in an agent's upload
    dir — the quota baseline a new batch is checked against."""
    if not agent_dir.is_dir():
        return 0, 0
    total = 0
    count = 0
    for p in agent_dir.iterdir():
        if p.is_file():
            total += p.stat().st_size
            count += 1
    return total, count


def render_safe_headers(name: str) -> dict[str, str]:
    """Extra response headers for serving an upload back. Renderable types get
    attachment + nosniff so a stored payload cannot execute in a browser;
    everything else gets nosniff alone (belt and suspenders — a content-type
    mismatch is then inert)."""
    headers = {"X-Content-Type-Options": "nosniff"}
    if Path(name).suffix.lower() in _RENDERABLE_SUFFIXES:
        headers["Content-Disposition"] = f'attachment; filename="{Path(name).name}"'
    return headers
