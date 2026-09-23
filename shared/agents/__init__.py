"""Shared agent contract — statuses, exception hierarchy, and the wire error protocol.

The contract implementation moved to `shared/agents/contract.py` (task #4570
R3, from the former root-module layout); this package also hosts `history/`.
This package door re-exports the old module API, so every existing
`shared.agents.<name>` caller keeps resolving.

Internal details (the `EXCEPTION_BY_REASON` registry and the exception classes'
own state) live in `shared/agents/contract.py`; code that patches them must
patch them on that module — a patch on a package attribute here is not seen by
the implementation's own reads.
"""

from shared.agents import history as history
from shared.agents.contract import (
    EXCEPTION_BY_REASON,
    AgentNotFound,
    AgentStatus,
    AvaAgentError,
    ChannelNotConfigured,
    CrossMachineGatewayUnavailable,
    ErrorReason,
    ForkCheckpointNotFound,
    ForkConfigChangeNotAllowed,
    ForkError,
    ForkSourceEmpty,
    GatewayUnavailable,
    IndexerUnavailable,
    InvalidModelConfig,
    MachineNotRegistered,
    MachinePaused,
    RestartResult,
    ResurrectAlreadyAlive,
    ResurrectBudgetExhausted,
    ResurrectError,
    ResurrectRefused,
    ResurrectResult,
    SpawnTargetNotAgentRunner,
    TerminateResult,
    TerminationSource,
)

__all__ = [
    "EXCEPTION_BY_REASON",
    "AgentNotFound",
    "AgentStatus",
    "AvaAgentError",
    "ChannelNotConfigured",
    "CrossMachineGatewayUnavailable",
    "ErrorReason",
    "ForkCheckpointNotFound",
    "ForkConfigChangeNotAllowed",
    "ForkError",
    "ForkSourceEmpty",
    "GatewayUnavailable",
    "IndexerUnavailable",
    "InvalidModelConfig",
    "MachineNotRegistered",
    "MachinePaused",
    "RestartResult",
    "ResurrectAlreadyAlive",
    "ResurrectBudgetExhausted",
    "ResurrectError",
    "ResurrectRefused",
    "ResurrectResult",
    "SpawnTargetNotAgentRunner",
    "TerminateResult",
    "TerminationSource",
    "history",
]
