"""Static checkpoint msgpack allowlist — the framework types that cross the
LangGraph checkpoint serde as pydantic-v2 ext objects.

Lives in `base` so both the agent runner (`agent/state.py` extends it with
dynamically registered plugin state classes) and the gateway cold-load reader
(`base/agents/history/checkpoint.py`) can build their `JsonPlusSerializer` allowlist
without crossing the import layering (`base` may not import `agent`).

LangGraph's `JsonPlusSerializer` deserializes a type only when it is named in
`allowed_msgpack_modules` (or in its built-in safe set). Without an explicit
allowlist the serializer runs permissive and warns on every checkpoint load —
"Deserializing unregistered type agent.state.* ... This will be blocked in a
future version" — once per type per process start; a future langgraph blocks
unregistered types outright.
"""

# (module, name) pairs. The nested sub-states of `agent.state_channels` are the
# only framework channel values serialized as pydantic-v2 ext objects today.
# Keep in sync with `agent/state.py` — `agent.state.checkpoint_msgpack_allowlist`
# starts from this set.
STATIC_CHECKPOINT_MSGPACK_TYPES: frozenset[tuple[str, str]] = frozenset(
    {
        ("base.agents.compaction.models", "CompactMarker"),
        ("base.agents.compaction.models", "CompactAcceptance"),
        ("base.agents.compaction.models", "CompactTarget"),
        ("base.agents.incarnation.native_work_models", "NativeWorkTarget"),
        ("base.agents.incarnation.native_work_models", "NativeCancelMarker"),
        ("agent.state_channels", "AttachState"),
        ("agent.state_channels", "AttachEntry"),
        ("agent.state_channels", "CompactState"),
        ("agent.state_channels", "MemoryState"),
        ("agent.state_channels", "ContextReset"),
        ("agent.state_channels", "CapabilitiesState"),
        ("agent.state_channels", "CircuitState"),
        ("agent.state_channels", "SkillRecallState"),
        # A list-channel element, not a sub-state: `BaseAgentState.security_findings`.
        ("base.agents.messages.security_finding", "SecurityFindingEntry"),
    }
)
