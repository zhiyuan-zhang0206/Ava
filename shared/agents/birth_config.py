"""Birth-frozen per-agent config — resolution at spawn, replay for life.

`shared/config` declares WHICH per-agent fields are frozen (the `lifecycle`
axis on the field registry). This module owns WHAT a frozen field resolves to
at the spawn boundary and WHERE that answer is kept.

## The three layers

Every per-agent field an agent process runs with comes from exactly one of::

    config_overlay   (agents_meta.config_overlay)  -- the user/spawner CHOSE this
    > birth_config   (agents_meta.birth_config)    -- the cluster default AT BIRTH
    > current config (.env / env / code default)   -- read live, this process start

The two stored maps are deliberately separate columns rather than one merged
blob: "the spawner picked claude-sonnet-5 for this worker" and "this worker was
born on the day the cluster default happened to be claude-sonnet-5" are
different facts, and collapsing them would make the second indistinguishable
from the first the moment anyone inspected the agent.

Only `frozen` fields are stamped. A `live` field is absent from birth_config on
purpose — its whole point is that the next process start re-reads it, so a
cluster edit reaches every existing agent. Plugin `Config` fields are outside
the framework registry and are never stamped; they behave as `live`.

## Why stamp at spawn rather than resolve lazily

Compact rebuilds an agent's system prompt from current config. Without a birth
stamp, flipping the cluster default model / skill set / communication style
would rewrite the identity material of every agent already alive, at whatever
arbitrary moment each next compacted. Freezing at birth makes a default flip
mean what an operator expects: it governs agents born after it.

## The cluster default model

`cluster_defaults` is a one-row table holding the cluster's chosen default
model. It is consulted ONLY here, at the spawn-time resolution of the frozen
`llm_model` — a running process still reads `settings.lm.llm_model` for its own
live purposes. When the row's value is NULL the resolution falls through to the
ordinary config chain (`.env` `AVA_MODEL`, then the code default), so a cluster
that never touched the control panel behaves exactly as before.

When the row IS set it wins over `.env`: the DB row is the cluster's deliberate
choice, made through `PUT /api/config/default-model`, and it must not be
silently dead on the (normal) cluster whose `.env` also carries an `AVA_MODEL`
line from install.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import psycopg

from shared.config import current_field_values, frozen_field_names


def cluster_default_model(cur: psycopg.Cursor) -> str | None:
    """The cluster's DB-backed default model, or None when unset.

    None means "no cluster-level choice" — the caller falls through to the
    ordinary config chain. A missing table is NOT tolerated: the row is created
    by the same migration that adds it, so its absence is a real schema drift
    the fail-fast posture wants surfaced.
    """
    cur.execute("SELECT llm_model FROM cluster_defaults WHERE id = 1")
    row = cur.fetchone()
    return row[0] if row else None


def set_cluster_default_model(cur: psycopg.Cursor, model: str, *, updated_by: str) -> None:
    """Set the cluster's default model. Callers validate `model` against the
    registry roster first — this writes what it is given."""
    cur.execute(
        "UPDATE cluster_defaults SET llm_model = %s, updated_at = now(), updated_by = %s "
        "WHERE id = 1",
        (model, updated_by),
    )


def _json_safe(value: Any) -> Any:
    """Coerce a config value into something JSONB can hold. Only Path needs it
    today; the coercion is here so a future Path-typed frozen field cannot
    silently serialize as a repr."""
    return str(value) if isinstance(value, Path) else value


def resolve_birth_config(
    cur: psycopg.Cursor,
    overlay: Mapping[str, object] | None = None,
    *,
    inherited: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """The frozen-field map to persist on a newly born agent's row.

    Args:
        cur: cursor on the transaction the agent row is being written in — used
            to read `cluster_defaults`.
        overlay: the explicit spawn `config_overlay`, if any. A frozen field
            present here is NOT stamped: the user's choice already lives in
            config_overlay and outranks birth_config, so duplicating it into
            both columns would destroy the provenance the split exists for.
        inherited: the parent's `birth_config` on a fork. A fork is the same
            identity continuing, so the parent's stamp is carried over verbatim
            rather than re-resolved against today's defaults. Frozen fields the
            parent's stamp does not cover (it predates the field) are resolved
            fresh, so an old agent's fork is still fully stamped.

    Returns:
        `{field name: value}` over `frozen_field_names()`. A value of None is
        kept, not dropped: for a per-model-defaultable field None is the "the
        cluster expressed no explicit opinion" sentinel, and freezing THAT is
        what keeps a later explicit cluster choice from reaching this agent.
    """
    stamped: dict[str, Any] = dict(inherited or {})
    overlay_keys = set(overlay or {})
    pending = [n for n in frozen_field_names() if n not in overlay_keys and n not in stamped]
    if not pending:
        return stamped
    # One .env read for the whole batch (current_field_values re-reads the file
    # so an edit since this process started is reflected).
    values = current_field_values()
    db_model = cluster_default_model(cur) if "llm_model" in pending else None
    for name in sorted(pending):
        if name == "llm_model" and db_model is not None:
            stamped[name] = db_model
            continue
        stamped[name] = _json_safe(values[name])
    return stamped
