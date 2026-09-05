"""Checkpoint-compatible plugin delta codec for external SDK attachments.

The existing serializer preserves reducer inputs exactly, including partial
mappings, sets, nested models and message objects. The JSON envelope fits the
lease's ordered delta journal; only the native graph writes checkpoints.
"""

from __future__ import annotations

import base64
import importlib
from typing import Any, cast

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import Connection, connect
from psycopg.rows import DictRow, dict_row

from shared.config import settings


def _state_module() -> Any:
    return importlib.import_module("agent.state")


def _serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        allowed_msgpack_modules=_state_module().checkpoint_msgpack_allowlist()
    )


def encode_plugin_delta(delta: dict[str, Any]) -> dict[str, str]:
    """Serialize a validated reducer input without coercing it to a field value."""
    state_module = _state_module()
    checked = state_module._validate_plugin_state_keys(delta, state_module.AgentState)
    encoding, payload = _serializer().dumps_typed(checked)
    return {"encoding": encoding, "data": base64.b64encode(payload).decode("ascii")}


def decode_plugin_delta(encoded: dict[str, Any]) -> dict[str, Any]:
    """Decode the checkpoint codec envelope, rejecting unknown state channels."""
    payload = _serializer().loads_typed(
        (encoded["encoding"], base64.b64decode(encoded["data"], validate=True))
    )
    if not isinstance(payload, dict):
        raise TypeError("external plugin delta must decode to a dict")
    state_module = _state_module()
    return cast(
        dict[str, Any], state_module._validate_plugin_state_keys(payload, state_module.AgentState)
    )


def apply_plugin_delta(state: Any, delta: dict[str, Any]) -> None:
    """Replay one journal entry through the registered field reducers."""
    state_module = _state_module()
    state_module._validate_plugin_state_keys(delta, type(state))
    for name, value in delta.items():
        reducer = state_module._resolve_reducer(state.__class__.model_fields[name])
        setattr(state, name, reducer(getattr(state, name), value))


def load_snapshot(agent_id: int) -> tuple[Any, dict[str, Any] | None, dict[str, Any] | None]:
    """Read native state and pinned config; never create or update a checkpoint."""
    with connect(
        settings.data_plane.db_url,
        autocommit=True,
        prepare_threshold=None,
        row_factory=cast(Any, dict_row),
    ) as conn:
        typed_conn = cast(Connection[DictRow], conn)
        row = typed_conn.execute(
            "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (agent_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"agent {agent_id} not found")
        saver = PostgresSaver(conn=typed_conn, serde=_serializer())
        checkpoint = saver.get({"configurable": {"thread_id": str(agent_id)}})
    if checkpoint is None:
        raise RuntimeError("approved agent has no checkpoint to attach")
    state_cls = _state_module().build_agent_state()
    state = state_cls.model_validate(checkpoint["channel_values"])
    return state, row["config_overlay"], row["birth_config"]
