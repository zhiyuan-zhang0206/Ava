"""Gateway HTTP request/response schemas, split by router family. Import from
a family module (`gateway.schemas.agents`) for a router's own surface; this
root re-exports every public model for cross-family + non-router consumers.
The gateway<->runner RPC-shared types live one layer down in `ops.rpc_schemas`
(import layering: shared < ops < gateway).

The response models the `cli` thin clients also decode — `MachineStatus` (roster)
and the `Config*` family — are downshifted a further layer to
`shared.api_contracts` (below both ops and gateway) and re-exported here under
their unchanged OpenAPI names, so `cli` validates them without importing up into
`gateway`.
"""

from gateway.schemas.agents import (
    AgentCompact,
    AgentRow,
    AgentSummary,
)
from gateway.schemas.cluster import (
    ClusterOpRequest,
)
from gateway.schemas.commands import (
    CommandItem,
)
from gateway.schemas.errors import (
    ErrorEnvelope,
)
from gateway.schemas.events import (
    AgentEventRow,
    AgentEventsResponse,
    EventRow,
    EventsMeta,
    EventsResponse,
)
from gateway.schemas.fleet_graph import (
    FleetGraphEdge,
    FleetGraphNode,
    FleetGraphResponse,
)
from gateway.schemas.frontend_telemetry import (
    FrontendInteractionIn,
    FrontendTelemetryBatch,
)
from gateway.schemas.inspect import (
    AgentActivity,
    AgentCost,
    AgentInspect,
    AgentInspectLive,
    AgentStats,
    AgentTps,
    HeartbeatInfo,
    HeartbeatLastPause,
    MetricPoint,
    NeighborRow,
    NeighborsResponse,
    PluginMetricResult,
)
from gateway.schemas.inventory import (
    InventoryAggregate,
    InventoryItem,
    InventoryItemAggregate,
    InventoryItemHostState,
    InventoryItemWriteResult,
    InventoryMachineView,
    InventoryWriteResult,
)
from gateway.schemas.memory import (
    MemoryGraphEdge,
    MemoryGraphNode,
    MemoryGraphResponse,
    MemoryNoteResponse,
    MemoryRefreshResponse,
    MemorySearchRequest,
    MemorySearchResponse,
    MemorySearchResultItem,
)
from gateway.schemas.messages import (
    AgentMessageEnqueued,
    AgentMessagesResponse,
    CancelRequest,
    CompactEnqueued,
    EscalationNoticeItem,
    LabelPatchRequest,
    LastMessageResponse,
    MessageEnqueued,
    NewThread,
    NoticeCreateIn,
    NoticeEditIn,
    NoticeItem,
    NoticesCursor,
    NoticesFeed,
    PendingInbound,
    ResolveNoticeIn,
    SystemNoteIn,
    TraceCheckpointMessagesResponse,
    UserMessageIn,
)
from gateway.schemas.models import (
    DefaultModelView,
    DefaultModelWrite,
    ModelInfo,
    ModelPricing,
    ModelsResponse,
)
from gateway.schemas.pages import (
    PageRegisterRequest,
)
from gateway.schemas.run_timeline import (
    RunTimelineBoundaries,
    RunTimelineEvent,
    RunTimelineExec,
    RunTimelineLlm,
    RunTimelineMeta,
    RunTimelineResponse,
    RunTimelineRow,
    RunTimelineWindow,
)
from gateway.schemas.shell import (
    ShellCaptureResponse,
)
from gateway.schemas.skills import (
    SkillEnableUpdate,
    SkillsView,
    SkillView,
)
from gateway.schemas.stats import (
    AgentMetricsItem,
    AgentMetricsReport,
    ContextBreakdownResponse,
    ContextCategory,
    ContextSection,
    MetricsMeta,
    MetricsReport,
    StatsDashboard,
    StatsTokens,
    StatsWindowHours,
    TokenUsageResponse,
    applied_window,
    window_delta,
)
from gateway.schemas.status import (
    AgentMachineRow,
    ClusterPanel,
    MachineDeleteResponse,
    MachinePauseRequest,
    MachinePauseResponse,
    MachineResumeResponse,
    ServiceItem,
    ServicesStatus,
    SystemStatus,
)
from gateway.schemas.tasks import (
    TaskListResponse,
    TaskRow,
    TaskSummaryRow,
    TaskUpdateRequest,
)
from gateway.schemas.ui_contributions import (
    UiContributionsResponse,
    UiNavContribution,
    UiThemeContribution,
)
from gateway.schemas.uploads import (
    UploadedBatch,
    UploadedFile,
)
from gateway.schemas.user_settings import (
    UserSettingListResponse,
    UserSettingRow,
    UserSettingUpdateRequest,
)

# API contract types downshifted to shared so `cli` decodes them too (re-exported
# here under their unchanged OpenAPI schema names).
from shared.api_contracts import (
    ConfigFieldView,
    ConfigFieldWriteResult,
    ConfigView,
    ConfigWriteResult,
    MachineStatus,
    ResolvedConfigView,
    ResolvedFieldView,
)

__all__ = [
    "AgentActivity",
    "AgentCompact",
    "AgentCost",
    "AgentEventRow",
    "AgentEventsResponse",
    "AgentInspect",
    "AgentInspectLive",
    "AgentMachineRow",
    "AgentMessageEnqueued",
    "AgentMessagesResponse",
    "AgentMetricsItem",
    "AgentMetricsReport",
    "AgentRow",
    "AgentStats",
    "AgentSummary",
    "AgentTps",
    "CancelRequest",
    "ClusterOpRequest",
    "ClusterPanel",
    "CommandItem",
    "CompactEnqueued",
    "ConfigFieldView",
    "ConfigFieldWriteResult",
    "ConfigView",
    "ConfigWriteResult",
    "ContextBreakdownResponse",
    "ContextCategory",
    "ContextSection",
    "DefaultModelView",
    "DefaultModelWrite",
    "ErrorEnvelope",
    "EscalationNoticeItem",
    "EventRow",
    "EventsMeta",
    "EventsResponse",
    "FleetGraphEdge",
    "FleetGraphNode",
    "FleetGraphResponse",
    "FrontendInteractionIn",
    "FrontendTelemetryBatch",
    "HeartbeatInfo",
    "HeartbeatLastPause",
    "InventoryAggregate",
    "InventoryItem",
    "InventoryItemAggregate",
    "InventoryItemHostState",
    "InventoryItemWriteResult",
    "InventoryMachineView",
    "InventoryWriteResult",
    "LabelPatchRequest",
    "LastMessageResponse",
    "MachineDeleteResponse",
    "MachinePauseRequest",
    "MachinePauseResponse",
    "MachineResumeResponse",
    "MachineStatus",
    "MemoryGraphEdge",
    "MemoryGraphNode",
    "MemoryGraphResponse",
    "MemoryNoteResponse",
    "MemoryRefreshResponse",
    "MemorySearchRequest",
    "MemorySearchResponse",
    "MemorySearchResultItem",
    "MessageEnqueued",
    "MetricPoint",
    "MetricsMeta",
    "MetricsReport",
    "ModelInfo",
    "ModelPricing",
    "ModelsResponse",
    "NeighborRow",
    "NeighborsResponse",
    "NewThread",
    "NoticeCreateIn",
    "NoticeEditIn",
    "NoticeItem",
    "NoticesCursor",
    "NoticesFeed",
    "PageRegisterRequest",
    "PendingInbound",
    "PluginMetricResult",
    "ResolveNoticeIn",
    "ResolvedConfigView",
    "ResolvedFieldView",
    "RunTimelineBoundaries",
    "RunTimelineEvent",
    "RunTimelineExec",
    "RunTimelineLlm",
    "RunTimelineMeta",
    "RunTimelineResponse",
    "RunTimelineRow",
    "RunTimelineWindow",
    "ServiceItem",
    "ServicesStatus",
    "ShellCaptureResponse",
    "SkillEnableUpdate",
    "SkillView",
    "SkillsView",
    "StatsDashboard",
    "StatsTokens",
    "StatsWindowHours",
    "SystemNoteIn",
    "SystemStatus",
    "TaskListResponse",
    "TaskRow",
    "TaskSummaryRow",
    "TaskUpdateRequest",
    "TokenUsageResponse",
    "TraceCheckpointMessagesResponse",
    "UiContributionsResponse",
    "UiNavContribution",
    "UiThemeContribution",
    "UploadedBatch",
    "UploadedFile",
    "UserMessageIn",
    "UserSettingListResponse",
    "UserSettingRow",
    "UserSettingUpdateRequest",
    "applied_window",
    "window_delta",
]
