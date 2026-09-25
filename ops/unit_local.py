"""Unit-local references shared by the ops handlers.

One strict locator for a retained image's interpreter, one reader for the
installed machine identity, and one stat face for a payload's private request
path, shared by every ops handler that runs an entry from a retained image
(`cluster_prepare_facts`, `cluster_prepare_dispatch`, `cluster_bootstrap_hop`,
`cluster_normal_continue`): the announced digest selects an image, never
authorizes it -- strict resolution plus the escape check refuse a symlinked or
foreign root before anything runs under it -- and the entry's request path must
be canonical private unit state before any spawn.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from shared.runtime_release import ReleaseRejectedError


def candidate_interpreter(home: Path, artifact_digest: str) -> Path:
    """The retained image's interpreter, strictly under the unit home."""
    root = home / "releases" / artifact_digest
    try:
        if root.resolve(strict=True) != root:
            raise ReleaseRejectedError("announced candidate image is not canonical")
        interpreter = (root / "venv" / "bin" / "python").resolve(strict=True)
    except OSError as exc:
        raise ReleaseRejectedError(
            "announced candidate image is not retained on this unit"
        ) from exc
    if not interpreter.is_relative_to(root / "venv"):
        raise ReleaseRejectedError("candidate interpreter escapes its retained image")
    return interpreter


def machine_identity(home: Path) -> str:
    """This installed unit's machine name, read from its identity file."""
    machine = (home / "machine_name").read_text(encoding="utf-8").strip()
    if not machine:
        raise ReleaseRejectedError("installed unit machine identity is empty")
    return machine


# The request carries whole resolved contexts (observations, operations,
# challenges, the predecessor's identity); the CI assembler's request measures a
# few KiB. 64 KiB is the same shape bound `read_prepared_context` enforces on the
# child side, so a swapped or miswired path refuses here instead of surfacing as
# a child failure. KEEP (task #3696 exception inventory): a relay-shape guard
# fixed by the request's shape, not a tuning knob.
_MAX_REQUEST_BYTES = 64 * 1024


def private_unit_reference(text: str, home: Path) -> Path:
    """The payload's request path, verified as canonical private unit state.

    The same stat face as `_update_bootstrap._private_reference` (absolute,
    canonical, owned, 0600, inside `{home}/run`) plus the regular-file and size
    checks `read_prepared_context` pairs it with. Shared by the ops handlers
    that spawn an entry against one unit's request (`cluster_bootstrap_hop`,
    `cluster_normal_continue`): a path that fails it is a wiring fault, not a
    verdict about the request.
    """
    path = Path(text)
    try:
        info = path.stat() if path.is_absolute() and path.resolve(strict=True) == path else None
    except OSError:
        info = None
    if (
        info is None
        or path.parent != home / "run"
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
        or info.st_size > _MAX_REQUEST_BYTES
    ):
        raise ReleaseRejectedError("request must be a canonical private unit reference")
    return path
