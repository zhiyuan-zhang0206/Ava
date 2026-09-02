"""Read-only update preflight (`GET /api/cluster/update-check`).

Split out of `ops/cluster_deploy.py` to keep that module under the file-size
ceiling; the gateway imports this through the `ops` re-exports, so no caller
imports it directly.
"""

from __future__ import annotations

import subprocess

from pydantic import BaseModel

from ops.cluster_deploy import _REPO_ROOT
from shared.config import settings
from shared.repo_change import classify_change
from shared.running_sha import get as _get_running_sha
from shared.source_integrity import get as _get_installed_sha
from shared.source_integrity import installed_sha_needs_replay as _installed_sha_needs_replay

# Ceiling on each read-only git subprocess. `git fetch` against a wedged
# network has no natural bound; without this a hung fetch parks whatever
# thread runs it (and, pre-2026-08-03, the gateway event loop itself — the
# update-check endpoint polls every 30s).
_GIT_TIMEOUT_S = 15.0


class UpdateCheck(BaseModel):
    """`GET /api/cluster/update-check` response — how far the gateway's
    checkout is behind its track target, and which side a pull would restart.

    `behind` is the commit count from the last fully installed commit to the
    track target. `needs_replay` names the exceptional state where the fully
    installed commit is ahead of the running-commit bookmark: a rollout was
    interrupted between them, so zero commits behind is not an up-to-date
    verdict. A running commit ahead of installation is the normal fast-path
    state and does not need replay.

    `frontend_changed` / `backend_changed` mirror `ava cluster update`'s own
    classification so the UI can tell the user what a rollout would actually
    restart (docs-only diffs count as neither — a pull with nothing to restart).
    """

    behind: int
    frontend_changed: bool
    backend_changed: bool
    needs_replay: bool


def _git_ro(*args: str) -> str:
    """Run a read-only `git <args>` in the repo root; return stdout stripped.

    Raises:
        An orchestration-spawn failure is unrelated; a non-zero git exit raises RuntimeError so the
        endpoint surfaces a 500 (the gateway's own checkout is broken,
        which is an operator problem, not a normal state).
    """
    res = subprocess.run(  # noqa: S603 — args hardcoded git subcommands, no user input
        ["git", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"`git {' '.join(args)}` failed (rc={res.returncode}): {res.stderr.strip()!r}"
        )
    return res.stdout.strip()


def update_check() -> UpdateCheck:
    """Read-only preflight: is there anything to roll out, and what would it restart?

    Fetches the track target — origin/main in `latest` mode, the newest dated
    release tag in `releases` mode — then compares the fully installed commit
    against it. The running bookmark remains a directional cross-check: only an
    installed commit ahead of it is a half-deployed state needing replay. A
    running commit ahead is normal after a fast-path pull. Falls back to the
    running bookmark, then HEAD, for pre-bookmark installations. Pure read-only
    — no pull, no working-tree mutation — so the UI can poll it without side
    effects. Mirrors the classification `ava cluster update` runs internally
    before deciding what to restart.
    """
    installed_sha = _get_installed_sha()
    running_sha = _get_running_sha()
    needs_replay = _installed_sha_needs_replay(installed_sha, running_sha, repo=_REPO_ROOT)

    _git_ro("fetch", "origin", settings.general.track_branch)
    if settings.general.track_mode == "releases":
        _git_ro("fetch", "--tags", "origin")
        from shared.release_tags import pick_latest_tag

        tag = pick_latest_tag(_git_ro("tag", "--list").splitlines())
        if tag is None:
            # `releases` mode with no release cut yet: nothing to converge to.
            return UpdateCheck(
                behind=0,
                frontend_changed=False,
                backend_changed=False,
                needs_replay=needs_replay,
            )
        target_ref = tag.name
    else:
        target_ref = f"origin/{settings.general.track_branch}"
    baseline = installed_sha or running_sha or "HEAD"
    behind_out = _git_ro("rev-list", "--count", f"{baseline}..{target_ref}")
    behind = int(behind_out)
    if behind == 0:
        return UpdateCheck(
            behind=0,
            frontend_changed=False,
            backend_changed=False,
            needs_replay=needs_replay,
        )
    diff = _git_ro("diff", "--name-only", baseline, target_ref)
    paths = [line for line in diff.splitlines() if line.strip()]
    frontend, backend = classify_change(paths)
    return UpdateCheck(
        behind=behind,
        frontend_changed=frontend,
        backend_changed=backend,
        needs_replay=needs_replay,
    )
