"""The one full-commit-id gate for every update entrypoint (task #3270).

Issue #2343: an operator copied a 9-character sha from a status display, the
detached updater checked it out (git accepts prefixes), and the post-checkout
tree verification deep-failed comparing the FULL `rev-parse HEAD` against the
prefix -- stranding the hold. Entrypoints validate here (fail fast, name the
fix), and the target machine still normalizes through `git rev-parse --verify`
as defense in depth; nothing downstream ever compares a prefix to a full id.
"""

from __future__ import annotations

import re

# One pattern for every entrypoint: the 40-hex commit id, lowercase, whole
# string. Kept deliberately strict -- a prefix is the failure mode this gate
# exists to refuse.
FULL_SHA = re.compile(r"[0-9a-f]{40}")


def require_full_sha(value: str, *, entry: str) -> str:
    """Return `value` when it is a full 40-hex commit id; raise ValueError otherwise."""
    if FULL_SHA.fullmatch(value) is None:
        raise ValueError(
            f"{entry} needs the full 40-character commit id (got {value!r}); "
            "resolve a short sha with `git rev-parse <sha>` first"
        )
    return value
