"""Adds one package through a full registry ``mutate`` cycle; never imported by a production entry.

Run as ``python _mutator_child.py <repo-root> <home> <name> <marker> <slow|fast> <hold-seconds>``.

``save`` stages the new registry into a temp sibling and renames it over the real path, so the read
that built it and the rename that publishes it are two separate moments. The ``slow`` writer sits
between them (touching ``marker`` once inside that window); the ``fast`` one waits for the marker
and then runs its own cycle, aiming for the gap.
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

repo_root, home, name, marker_arg, role, hold_arg = sys.argv[1:7]
marker = pathlib.Path(marker_arg)
sys.path.insert(0, repo_root)
os.environ["AVA_HOME"] = home

if role == "slow":
    _real_replace = os.replace

    def _slow_replace(src: str, dst: str, **kw: object) -> None:
        if str(dst).endswith("installed.json"):
            marker.touch()
            time.sleep(float(hold_arg))
        return _real_replace(src, dst, **kw)  # type: ignore[arg-type]

    os.replace = _slow_replace  # type: ignore[assignment]

from base.packages.extensions import install_registry as reg  # noqa: E402

reg.load()  # pay the import + first-read cost before the handshake
if role == "fast":
    deadline = time.monotonic() + 60
    while not marker.exists():
        if time.monotonic() > deadline:
            raise SystemExit("the slow writer never reached its window")
        time.sleep(0.01)
with reg.mutate() as registry:
    registry.packages.append(reg.InstalledPackage(name=name, type="skill", source="https://x"))
