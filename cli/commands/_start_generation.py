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


def launch_digest(
    repo: Path, environment: dict[str, str], *, home: Path, runtime: StartRuntime | None = None
) -> str:
    """Bind source, transport environment and authoritative on-disk configuration."""
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
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
