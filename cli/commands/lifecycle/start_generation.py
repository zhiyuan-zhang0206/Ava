"""Read-only source identity for a development root generation.

The snapshot includes dirty and untracked source, not just HEAD. Ignored build
outputs and the editable environment are not sealed by this check.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from base.deploy.release import start_inputs
from base.deploy.release.runtime_interpreter import source_digest
from base.host.env.registry import launch_input_keys


def _write_generation(home: Path) -> dict[str, object] | None:
    """The delivered write generation's non-secret reference: the ledger's active
    generation on a gateway home, the installed unit capability's on a pure
    agent-runner, None on a remote-managed plane."""
    from base.cluster.authority import load_ledger
    from base.cluster.authority.unit import unit_reference

    ledger = load_ledger(home.resolve())
    if ledger is None:
        return unit_reference(home.resolve())
    if ledger.active is None:
        return None
    return {"number": ledger.active.number, "credential_digest": ledger.active.credential_digest}


def launch_digest(repo: Path, environment: dict[str, str], *, home: Path) -> str:
    """Bind source, the declared launch inputs of the transport environment,
    authoritative on-disk configuration and the delivered write generation (its
    number and credential digest only).

    Ambient keys a service manager or shell adds to the environment are not
    launch inputs (`launch_input_keys`), so the boot unit's start action and a
    fixed-environment observer derive the same digest for one launch.
    """
    inputs = launch_input_keys()
    payload = {
        "source": source_digest(repo),
        "environment": {key: value for key, value in environment.items() if key in inputs},
        "configuration": start_inputs.configuration_digest(home),
        "write_generation": _write_generation(home),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
