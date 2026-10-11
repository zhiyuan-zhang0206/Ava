"""Fresh-interpreter import-boundary probes for the cluster package; never imported by a production entry.

Run as ``python -I [-B] _import_child.py <scenario> <repo-root>``: neither scenario may load
Settings or the home while importing the cluster value types.
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


def cluster_package_modules() -> None:
    import base.cluster  # pyright: ignore[reportUnusedImport]
    import base.cluster.ownership  # pyright: ignore[reportUnusedImport]
    import base.cluster.port_preflight  # noqa: F401  # pyright: ignore[reportUnusedImport]

    assert "base.cluster.machine" in sys.modules
    loaded = [
        n for n in ("base.config", "base.paths", "base.cluster.machine.boot") if n in sys.modules
    ]
    assert not loaded, loaded


SCENARIOS = {
    "machine_value_imports": machine_value_imports,
    "cluster_package_modules": cluster_package_modules,
}

if __name__ == "__main__":
    sys.path.insert(0, sys.argv[2])
    SCENARIOS[sys.argv[1]]()
