"""Unit-local references shared by the ops handlers.

One strict locator for a retained image's interpreter and one reader for the
installed machine identity, shared by every ops handler that runs an entry from
a retained image (`cluster_prepare_facts`, `cluster_prepare_dispatch`,
`cluster_bootstrap_hop`): the announced digest selects an image, never
authorizes it -- strict resolution plus the escape check refuse a symlinked or
foreign root before anything runs under it.
"""

from __future__ import annotations

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
