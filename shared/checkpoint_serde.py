"""Static checkpoint msgpack allowlist — the framework types that cross the
LangGraph checkpoint serde as pydantic-v2 ext objects.

Lives in `shared` so both the agent runner (`agent/state.py` extends it with
dynamically registered plugin state classes) and the gateway cold-load reader
(`shared/checkpoint.py`) can build their `JsonPlusSerializer` allowlist
without crossing the import layering (`shared` may not import `agent`).

LangGraph's `JsonPlusSerializer` deserializes a type only when it is named in
`allowed_msgpack_modules` (or in its built-in safe set). Without an explicit
allowlist the serializer runs permissive and warns on every checkpoint load —
"Deserializing unregistered type agent.state.* ... This will be blocked in a
future version" — once per type per process start; a future langgraph blocks
unregistered types outright.
"""

# (module, name) pairs. `agent.state`'s five nested sub-states are the only
# framework channel values serialized as pydantic-v2 ext objects today; the
# dynamic `AgentState` subclass name is registered defensively (the state
# object itself is not a channel value, but naming it costs nothing and
# survives a future format change). Keep in sync with `agent/state.py` —
# `agent.state.checkpoint_msgpack_allowlist` starts from this set.
STATIC_CHECKPOINT_MSGPACK_TYPES: frozenset[tuple[str, str]] = frozenset(
    {
        # Legacy pairs: checkpoints written before the issue #156 split carry
        # `("agent.state", ...)` envelopes; `agent.state` re-exports the models
        # from `agent.state_channels`, so these must stay for old checkpoints to
        # keep deserializing.
        ("agent.state", "AttachState"),
        ("agent.state", "AttachEntry"),
        ("agent.state", "CompactState"),
        ("agent.state", "MemoryState"),
        ("agent.state", "ContextReset"),
        ("agent.state", "CapabilitiesState"),
        ("agent.state", "CircuitState"),
        ("agent.state", "AgentState"),
        # Current pairs: freshly-written checkpoints carry the real module.
        ("agent.state_channels", "AttachState"),
        ("agent.state_channels", "AttachEntry"),
        ("agent.state_channels", "CompactState"),
        ("agent.state_channels", "MemoryState"),
        ("agent.state_channels", "ContextReset"),
        ("agent.state_channels", "CapabilitiesState"),
        ("agent.state_channels", "CircuitState"),
    }
)
