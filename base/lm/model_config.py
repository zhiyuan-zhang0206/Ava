"""Validate the effective model configuration before agent configuration writes."""

from collections.abc import Mapping

import psycopg

from base.agents import AgentNotFound, InvalidModelConfig
from base.agents.birth_config import resolve_birth_config
from base.config.agent_pins import resolve_agent_config_pins
from base.host.env.agent_slices import agent_setting
from base.lm import factory


def validate_spawn_model_config(
    cur: psycopg.Cursor,
    config: dict[str, object] | None,
    fork_from: int | None = None,
) -> str:
    """Check the model and effort a plain birth or inherited fork will run."""
    inherited = None
    if fork_from is not None:
        cur.execute("SELECT birth_config FROM agents_meta WHERE id=%s", (fork_from,))
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"fork source agent {fork_from} does not exist")
        inherited = row[0]
    try:
        birth = resolve_birth_config(cur, config, inherited=inherited)
        return factory.validate_model_config(config=resolve_agent_config_pins(config, birth))
    except ValueError as exc:
        raise InvalidModelConfig(str(exc)) from exc


def validate_restart_model_config(
    cur: psycopg.Cursor, agent_id: int, overlay: Mapping[str, object]
) -> None:
    """Lock and check overlay > stored overlay > birth before changing either field.

    The caller holds a writable transaction through its overlay and restart
    writes. Provider credentials are checked on launch, not on this config edit.
    """
    if not {"llm_model", "reasoning_effort"}.intersection(overlay):
        return
    cur.execute(
        "SELECT config_overlay,birth_config FROM agents_meta WHERE id=%s FOR UPDATE",
        (agent_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    stored_overlay: dict[str, object] | None = row[0]
    birth_config: dict[str, object] | None = row[1]
    merged = {**(stored_overlay or {}), **overlay}
    pins = resolve_agent_config_pins(merged, birth_config)
    try:
        factory.validate_model_config(
            model=agent_setting("llm_model", pins), config=pins, check_provider_key=False
        )
    except ValueError as exc:
        raise InvalidModelConfig(str(exc)) from exc
