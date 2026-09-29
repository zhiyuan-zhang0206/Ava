---
type: doc
title: Gateway Router Design Principles
description: What every gateway router may and may not do — the dependency direction (never `agent.*`), the no-turn-loop rule and its enumerated exception, the handler/mounting split, and boundary typing.
tags:
- gateway
---

# Gateway Router Design Principles

- Each router is an independent file, depending on pure functions from `base.*`/`ops.*` plus gateway internals (`gateway.events.sse`, `gateway.agents.delivery`, lazy `gateway.app` `db_pool`, spawn forwarding in `gateway.agents.forward`); **never** imports `agent.*`
- **Feature packages**: a surface whose routes, helpers and wire models change together lives in one package (`gateway/<feature>/`) behind a docstring-only door; callers outside it import its public modules, never its `_`-prefixed ones
- Gateway itself does not build the turn-loop prompts, run LLMs, or construct LangGraph (the spawn/draft endpoints — schedules/guide/packages — inline a fixed system prompt for the agent they spawn; that is the deliberate, enumerated exception)
- Endpoint implementation and mounting are separated: router defines handlers, app.py is responsible for `include_router`
- **Boundary typing**: request bodies are pydantic models (a feature package's `schemas.py`, or `gateway/schemas/<domain>.py` for a `gateway/routers/` module), not bare `dict[str, object]`; cross-machine RPC wire dicts are immediately `Model.model_validate()`d into named types in `ops/rpc_schemas/__init__.py` (`ConfigReadResult`, `InventoryReadResult`, …)—handlers use attribute access, no bare `dict.get()`/`row["key"]`
