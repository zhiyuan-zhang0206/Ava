"""Fresh-interpreter import-boundary probes for the cluster package; never imported by a production entry.

Run as ``python -I [-B] _import_child.py <scenario> <repo-root>``: the scenario must not load
Settings while importing the machine value types.
"""

from __future__ import annotations

import importlib.abc
import sys


def machine_value_imports() -> None:
    class SettingsForbidden(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
            if fullname == "base.config" or fullname.startswith("base.config."):
                raise AssertionError("value imports must not load settings")

    sys.meta_path.insert(0, SettingsForbidden())
    from base.cluster.machine import (
        MachineIdentity,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    )
    from base.telemetry import EventPipeline  # noqa: F401  # pyright: ignore[reportUnusedImport]

    assert "base.config" not in sys.modules


SCENARIOS = {
    "machine_value_imports": machine_value_imports,
}

if __name__ == "__main__":
    sys.path.insert(0, sys.argv[2])
    SCENARIOS[sys.argv[1]]()
