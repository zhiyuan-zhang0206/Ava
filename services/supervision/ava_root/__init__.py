"""The root owner for one home's application services and native custody.

macOS starts this tree under the stable signed permissions helper. Transport,
health observation, and service manifests share this owner; there is no service
session mode or in-place interpreter upgrade.
"""

from base.native_process.root_control.ipc import UnitState
from services.supervision.ava_root.health import HealthConfig, HealthMonitor
from services.supervision.ava_root.manifest import (
    ROOT_ID,
    ManifestError,
    RestartPolicy,
    UnitManifest,
    UnitRegistry,
    UnknownUnitError,
    load_manifests,
)
from services.supervision.ava_root.probes import ProbeError, ProbeRegistry
from services.supervision.ava_root.selfcheck import SelfCheckConfig, TreeSelfCheck
from services.supervision.ava_root.server import ControlServer
from services.supervision.ava_root.singleton import (
    AlreadyRunningError,
    acquire_instance_lock,
    release_instance_lock,
)
from services.supervision.ava_root.supervisor import Supervisor, SupervisorConfig
from services.supervision.ava_root.wiring import (
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
