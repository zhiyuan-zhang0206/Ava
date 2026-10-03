"""Trunk merge-queue HTTP API and PR base-freshness helpers shared by the CI tools."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

# The Ava checkout root this module ships in — anchors base-freshness git reads
# against THIS repo's origin regardless of the caller's cwd (task #2496).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

API_BASE_URL = "https://api.trunk.io/v1"
REQUEST_TIMEOUT_SECONDS = 30
ORG_SLUG = "ava"


def target_payload(repo: str) -> dict[str, object]:
    """Build the repository and target branch identity Trunk endpoints share."""
    owner, name = repo.split("/", maxsplit=1)
    return {
        "repo": {"host": "github.com", "owner": owner, "name": name},
        "targetBranch": "main",
    }


def pr_payload(pr: str, repo: str) -> dict[str, object]:
    """Build the repository, PR, and target branch identity Trunk requires."""
    payload = target_payload(repo)
    payload["pr"] = {"number": int(pr)}
    return payload


_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_sha(value: str) -> bool:
    return bool(_SHA1_RE.match(value) or _SHA256_RE.match(value))


def base_freshness(pr: str, repo: str) -> tuple[tuple[str, str] | None, bool]:
    """Return ((base_sha, main_sha) when stale else None, unreadable).
    The PR's `baseRefOid` is the base-branch SHA GitHub last evaluated the PR against. When
    current main is ahead of it, the queue's predictive branch will include commits this PR's green
    CI never saw, so Trunk re-tests the tree against the newer base — the extra in-queue round task
    #2496 (A1) wants operators warned about. Advisory only: any read error or non-SHA output sets
    `unreadable` and never blocks submission.
    """
    result = subprocess.run(  # noqa: S603
        ["gh", "pr", "view", pr, "--repo", repo, "--json", "baseRefOid", "--jq", ".baseRefOid"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None, True
    base_sha = result.stdout.strip()
    if not _is_sha(base_sha):
        return None, True
    result = subprocess.run(  # noqa: S603
        # -C anchors the checkout: cwd can be another git repo (e.g. the memory
        # pool), whose origin would answer with a VALID sha and mis-refuse in
        # require mode (QA NIT, 2026-09-06).
        ["git", "-C", str(_REPO_ROOT), "ls-remote", "origin", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None, True
    main_sha = result.stdout.split(maxsplit=1)[0]
    if not _is_sha(main_sha):
        return None, True
    if base_sha == main_sha:
        return None, False
    return (base_sha, main_sha), False


def post(
    endpoint: str, payload: dict[str, object], token: str
) -> tuple[dict[str, object] | None, str | None]:
    """POST one Trunk API request, returning its object response or an error."""
    request = urllib.request.Request(  # noqa: S310 - fixed HTTPS Trunk API endpoint
        f"{API_BASE_URL}/{endpoint}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - request uses the fixed HTTPS endpoint above
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as error:
        # urllib raises HTTPError (a URLError subclass) instead of returning
        # the response for 4xx/5xx, and its str() is "HTTP Error 409: ..." —
        # normalize to "HTTP <code>" so callers can match statuses exactly.
        return None, f"HTTP {error.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return None, str(error)
    if status != 200:
        return None, f"HTTP {status}"
    if not body:
        return {}, None
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        # submitPullRequest / cancelPullRequest answer 200 with a plain-text
        # "OK" body (verified live 2026-09-01); a 200 is a success regardless
        # of body shape, so a non-JSON body must not be read as an error.
        return {}, None
    if not isinstance(data, dict):
        return None, "response was not a JSON object"
    return data, None
