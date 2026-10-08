"""The context-breakdown wire models and the response built from one request's breakdown.

`ContextBreakdownResponse` is the body of `GET /api/agents/{id}/context-breakdown` and the base of
the run timeline's per-request context. Both the gateway and the insights service build it, so the
models and the budget resolution (the agent's model window and compaction thresholds) live here
rather than in either of them.
"""

import logging
from typing import Any

from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from base.agents.history.context_breakdown import RequestBreakdown, SectionNode
from base.agents.model_overrides import read_agent_overrides
from base.config import settings
from base.lm.context_budget import UnknownModelWindowError, resolve_context_budget
from base.lm.registry import resolve_available_model

_log = logging.getLogger(__name__)


class ContextSection(BaseModel):
    """One node of the system prompt's recursive section breakdown: a heading — or
    the `(preamble)` / `(intro)` prose before the first sub-heading — with its
    token share of the system prompt (always estimated: a section is never measured alone). A section over ~1000 tokens is drilled into its
    next-level sub-headings as `children` (recursively); a smaller section, or one
    with no deeper heading, is a leaf (`children == []`). When `children` is
    non-empty their tokens sum to this node's `tokens`."""

    name: str
    tokens: int
    estimated: bool = True
    children: list["ContextSection"] = Field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]


class ContextCategory(BaseModel):
    """One context bucket (system_prompt / compact_summary / cluster_memory /
    agent_memory / context_note / user_input / agent_messages / automation /
    reasoning / output / tool_call / tool_response) with its token count: the sum of its
    messages' own counts. `estimated` is true when any part was a share of a provider total
    rather than the provider's own number (the UI appends "(estimated)"); `exact_fraction` is the
    share of `tokens` that was exact. Inbound messages split by source: user_input (a human
    turn), agent_messages (a peer agent), automation (a machine/framework wakeup)."""

    kind: str
    tokens: int
    estimated: bool
    exact_fraction: float


class ContextBreakdownResponse(BaseModel):
    """GET /api/agents/{id}/context-breakdown — how the agent's context window is
    spent, for the composer's breakdown panel (lazy-loaded when the panel opens).

    The breakdown of the latest LLM request's input. Each message's tokens are anchored to
    the provider's reported `input_tokens` (see `base/agents/history/message_tokens.py`), so
    `categories` sum exactly to `total_input_tokens`; only the inside of a message
    (reasoning / output / tool_call, system-prompt sections) is split by an estimator.
    `estimated` / `exact_fraction` say whether any part of the total was estimated and how much
    of it was the provider's own number. `sections` break down the `system_prompt` category.
    With no LLM request yet the total is 0 and nothing is listed. `max_input_tokens` /
    `soft_compact_tokens` / `hard_compact_tokens` mirror the token-usage endpoint (0 when the
    model window is unknown)."""

    total_input_tokens: int
    estimated: bool
    exact_fraction: float
    max_input_tokens: int = 0
    soft_compact_tokens: int = 0
    hard_compact_tokens: int = 0
    sections: list[ContextSection]
    categories: list[ContextCategory]


def resolve_agent_model(pool: ConnectionPool[Any], agent_id: int) -> str:
    """The agent's effective LLM model: its per-agent overlay, else the cluster default
    (`settings.lm.llm_model`), resolved through any withdrawal fallback so callers judge the model
    that will actually run. Capability gates (image input) must use this resolved id, never the raw
    configured one."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    overlay = row[0] if row and row[0] else None
    model = overlay.get("llm_model") if overlay else None
    return resolve_available_model(model or settings.lm.llm_model)


def _to_context_section(node: SectionNode) -> ContextSection:
    return ContextSection(
        name=node.name,
        tokens=node.tokens,
        estimated=node.estimated,
        children=[_to_context_section(child) for child in node.children],
    )


def context_breakdown_response(
    pool: ConnectionPool[Any], agent_id: int, breakdown: RequestBreakdown
) -> ContextBreakdownResponse:
    """`breakdown` (one LLM request's input) with the agent's resolved window and compaction
    thresholds; the thresholds are 0 when the agent's model has no known window."""
    model = resolve_agent_model(pool, agent_id)
    overrides = read_agent_overrides(pool, agent_id)
    max_input_tokens = soft_compact_tokens = hard_compact_tokens = 0
    try:
        budget = resolve_context_budget(model, overrides)
        max_input_tokens = budget.max_context_tokens
        soft_compact_tokens = budget.soft_compact_tokens
        hard_compact_tokens = budget.hard_compact_tokens
    except UnknownModelWindowError as exc:
        _log.warning("context-breakdown: %s", exc)

    return ContextBreakdownResponse(
        total_input_tokens=breakdown.total.tokens,
        estimated=breakdown.total.estimated,
        exact_fraction=breakdown.total.exact_fraction,
        max_input_tokens=max_input_tokens,
        soft_compact_tokens=soft_compact_tokens,
        hard_compact_tokens=hard_compact_tokens,
        sections=[_to_context_section(node) for node in breakdown.sections],
        categories=[
            ContextCategory(
                kind=c.kind,
                tokens=c.total.tokens,
                estimated=c.total.estimated,
                exact_fraction=c.total.exact_fraction,
            )
            for c in breakdown.categories
        ],
    )
