"""ava-root: the platform-neutral root supervisor skeleton.

One tree of long-lived processes per (machine x home). This package owns the
tree's lifecycle (spawn, stop, restart policies), the K1 control plane over a
unix socket, and the K2 unit-manifest registry. What launches the root process
by itself (the OS edge) is out of scope here by design: the tree's own code
carries no platform-specific concepts.

Not wired into the cluster yet — nothing else imports this package, and it is
exercised by its own tests only (`tests/services/test_ava_root_*.py`).
"""

from services.ava_root.client import RootClient, RootClientError
from services.ava_root.manifest import (
    ROOT_ID,
    ManifestError,
    RestartPolicy,
    UnitManifest,
    UnitRegistry,
    UnknownUnitError,
    load_manifests,
)
from services.ava_root.server import ControlServer
from services.ava_root.singleton import (
    AlreadyRunningError,
    acquire_instance_lock,
    release_instance_lock,
)
from services.ava_root.supervisor import Supervisor, SupervisorConfig, UnitState

__all__ = [
    "ROOT_ID",
    "AlreadyRunningError",
    "ControlServer",
    "ManifestError",
    "RestartPolicy",
    "RootClient",
    "RootClientError",
    "Supervisor",
    "SupervisorConfig",
    "UnitManifest",
    "UnitRegistry",
    "UnitState",
    "UnknownUnitError",
    "acquire_instance_lock",
    "load_manifests",
    "release_instance_lock",
]
