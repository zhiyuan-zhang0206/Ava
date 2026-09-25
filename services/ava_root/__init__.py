"""The root owner for one home's application services and native custody.

macOS starts this tree under the stable signed permissions helper. Transport,
health observation, and service manifests share this owner; there is no service
session mode or in-place interpreter upgrade.
"""

from services.ava_root.client import RootClient, RootClientError
from services.ava_root.health import HealthConfig, HealthMonitor
from services.ava_root.manifest import (
    ROOT_ID,
    ManifestError,
    RestartPolicy,
    UnitManifest,
    UnitRegistry,
    UnknownUnitError,
    load_manifests,
)
from services.ava_root.probes import ProbeError, ProbeRegistry
from services.ava_root.selfcheck import SelfCheckConfig, TreeSelfCheck
from services.ava_root.server import ControlServer
from services.ava_root.singleton import (
    AlreadyRunningError,
    acquire_instance_lock,
    release_instance_lock,
)
from services.ava_root.supervisor import Supervisor, SupervisorConfig, UnitState
from services.ava_root.wiring import (
    WiringContext,
    WiringError,
    WiringParticipant,
    load_wiring,
)

__all__ = [
    "ROOT_ID",
    "AlreadyRunningError",
    "ControlServer",
    "HealthConfig",
    "HealthMonitor",
    "ManifestError",
    "ProbeError",
    "ProbeRegistry",
    "RestartPolicy",
    "RootClient",
    "RootClientError",
    "SelfCheckConfig",
    "Supervisor",
    "SupervisorConfig",
    "TreeSelfCheck",
    "UnitManifest",
    "UnitRegistry",
    "UnitState",
    "UnknownUnitError",
    "WiringContext",
    "WiringError",
    "WiringParticipant",
    "acquire_instance_lock",
    "load_manifests",
    "load_wiring",
    "release_instance_lock",
]
