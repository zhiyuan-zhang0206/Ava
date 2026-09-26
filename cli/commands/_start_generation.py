"""Read-only source identity for a development root generation.

The snapshot includes dirty and untracked source, not just HEAD. Ignored build
outputs and the editable environment are not sealed by this development check;
production release admission must verify its complete immutable artifact.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cli.start_runtime import StartRuntime
from shared import start_inputs
from shared.runtime_interpreter import source_digest


def _write_generation(home: Path) -> dict[str, object] | None:
    """The active write generation's non-secret reference, or None when the home
    keeps no database authority (a pure runner or a remote-managed plane)."""
    from shared.cluster.authority import load_ledger

    ledger = load_ledger(home.resolve())
    if ledger is None or ledger.active is None:
        return None
    return {"number": ledger.active.number, "credential_digest": ledger.active.credential_digest}


def launch_digest(
    repo: Path, environment: dict[str, str], *, home: Path, runtime: StartRuntime | None = None
) -> str:
    """Bind source, transport environment, authoritative on-disk configuration and
    the delivered write generation (its number and credential digest only)."""
    payload = {
        "source": source_digest(repo)
        if runtime is None or runtime.release is None
        else {
            "artifact": runtime.release.digest,
            "manifest": runtime.release.manifest_digest,
            "commit": runtime.source_commit,
        },
        "environment": environment,
        "configuration": start_inputs.configuration_digest(home),
        "write_generation": _write_generation(home),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
