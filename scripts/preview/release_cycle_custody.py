"""Capture application generations before transitions and prove native closure."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts.preview import local
from shared.native_process.ownership import OwnedProcess, capture_tree
from shared.verified_file import regular_bytes


def capture(run: Path, label: str) -> None:
    encoded = regular_bytes(run / f"cycle-release-{label}.json")
    snapshot = json.loads(encoded)
    if snapshot["result"] != "passed":
        raise RuntimeError("cannot capture an unverified application generation")
    root = OwnedProcess(**snapshot["births"]["root"])
    members = capture_tree(root)
    leaders = {OwnedProcess(**row) for row in snapshot["births"].values()}
    if not {item.birth_key() for item in leaders} <= {item.birth_key() for item in members} or any(
        not identity.live() for identity in leaders
    ):
        raise RuntimeError("application generation changed before custody capture")
    local.write_json(
        run / f"release-apps-{label}.json",
        {
            "snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
            "at": time.time(),
            "members": [asdict(row) for row in sorted(members, key=lambda row: row.pid)],
        },
    )


def closed(run: Path, label: str) -> None:
    encoded = regular_bytes(run / f"release-apps-{label}.json")
    captured = json.loads(encoded)
    snapshot = regular_bytes(run / f"cycle-release-{label}.json")
    if hashlib.sha256(snapshot).hexdigest() != captured["snapshot_sha256"]:
        raise RuntimeError("prior application snapshot changed after custody capture")
    observations: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "custody_sha256": hashlib.sha256(encoded).hexdigest(),
        "at": time.time(),
        "observations": observations,
        "result": "running",
    }
    try:
        for row in captured["members"]:
            identity = OwnedProcess(**row)
            observations.append({"identity": row, "live": identity.live()})
        if not observations or any(row["live"] for row in observations):
            raise RuntimeError("prior application generation has not closed")  # noqa: TRY301 — retain native observations before failing
        evidence["result"] = "passed"
    except BaseException as exc:
        evidence.update(result="failed", error=repr(exc))
        raise
    finally:
        local.write_json(run / f"release-apps-{label}-closed.json", evidence)
