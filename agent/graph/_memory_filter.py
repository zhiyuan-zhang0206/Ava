"""The relevance filter passive recall runs between retrieval and injection.

Vector search always returns its top-k. However weak the match, notes come back
— so recall without a filter injects its top-k every turn, and a note that
merely shares a word with the question arrives looking like context the agent
should act on. The filter decides which retrieved notes are worth the agent's
attention. Its bias is to *list*: a note the agent ignores costs nothing (it
reads a listed path only when it looks useful), while a note dropped wrongly is
gone — so when in doubt the filter includes a note rather than rejecting it. On
a turn where nothing retrieved fits, the right answer is still an empty list.

It judges names and one-line descriptions, never note bodies, so it stays a fast
call on a modest model. Its one real rule beyond relevance is about the notes that
describe the user or a project: those match on subject matter, not on sharing a
word with the question — a profile saying "works on database performance" is not
relevant to every question containing "performance". That is the failure mode a
`type/<x>` tag exists to make visible to this filter.

Failure is not fatal, but it is also not silent degradation. Recall is an
enhancement running on someone else's turn, so a model error, a timeout, or a
malformed reply must not raise into it — yet the fallback is to inject *nothing*,
not the unfiltered top matches. Unfiltered top-k is exactly the failure mode this
filter exists to remove — a note that merely shares a word arrives looking like
context to act on — so surfacing it when the judge fails would make a broken
filter indistinguishable from a working one (that silent degradation is what
review F1 caught: the registry's max-effort default timed every call out and
recall has injected unfiltered top-3 since launch). A path the model invents that
was not among the candidates is dropped rather than raised on: the fail-fast rule
is about the agent's own mistakes in `execute_code`, not about a helper model on
a background path, where taking the whole turn down is the worse outcome.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass

from langchain_core.messages import HumanMessage

from shared.config.turn_view import turn_settings
from shared.log import logger

_LABEL = "recall-filter"
_PICKED_PATH_SAMPLE_LIMIT = 10
_RECALL_LOG_HMAC_KEY = secrets.token_bytes(32)

# The judging-call bound is configurable (AVA_MEMORY_RECALL_FILTER_TIMEOUT_SECONDS,
# task #698 G8). It sits in front of the agent's turn, so a slow filter is a slow
# agent. The model is built with reasoning pinned off (see filter_candidates), so
# a healthy call lands in the low seconds and this bound only fires on a
# genuinely wedged provider.

_INSTRUCTION = """You are choosing which of an agent's long-term notes to surface for the
conversation below.

Return ONLY a JSON array of the paths worth surfacing, most relevant first, at
most {inject_k}. Return [] only when the conversation is clearly unrelated to
every single note.

List generously - err on the side of including. The agent skims listed notes and
reads only what looks useful, so an extra note costs almost nothing; a note you
omit is never seen. Omitting a useful note is the only real mistake.
When in doubt, list the note.

Judge what the conversation is ABOUT, not word overlap. The conversation and the
notes may each be in Chinese or English - relevance crosses languages. When the
conversation references an agent id, a role, or a person, notes describing the
organization, roles, or people are likely relevant - a role name like
"intel chief" or "researcher" identifies a member of the
organization, and the note mapping roles to members fits even when a word like
"cluster" or "org" also appears in the conversation.
Notes tagged type/user or type/project describe the user and their projects
generally; they fit when the conversation is about that topic, not merely
because a word recurs.

Conversation:
{query}

Notes:
{candidates}
"""


@dataclass(frozen=True)
class Candidate:
    """One retrieved note as the filter sees it: what search returned, nothing
    more. `tags` carries the note's `type/<x>`, which is what lets the filter be
    stricter with profile notes than with procedures."""

    path: str
    description: str
    tags: list[str]


def _render(candidates: list[Candidate]) -> str:
    lines: list[str] = []
    for c in candidates:
        tags = f" [{', '.join(c.tags)}]" if c.tags else ""
        desc = c.description or "(no description)"
        lines.append(f"- {c.path}{tags}: {desc}")
    return "\n".join(lines)


def _parse(reply: str, allowed: set[str]) -> list[str] | None:
    """The paths in a model reply, or None when it cannot be read as a list.

    Tolerates the wrappers small models add — a code fence, a sentence of
    preamble, or the array nested under a key — by taking the first bracketed
    span. Anything outside `allowed` is dropped with a warning: a path the model
    invented cannot be injected, and the rest of its answer is still usable.
    """
    match = re.search(r"\[.*?\]", reply, re.DOTALL)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None

    picked: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            continue
        if item not in allowed:
            # A path the model invented is routine LLM noise: it is dropped
            # and the rest of the reply still applies, so debug not warning.
            logger.debug(
                "[{label}] {body}",
                label=_LABEL,
                body="model returned an unknown path",
                event="recall_filter",
            )
            continue
        if item not in picked:
            picked.append(item)
    return picked


def _log_filter_decision(query: str, candidates: list[Candidate], picked: list[str]) -> None:
    """Emit one privacy-preserving, bounded record of a recall verdict.

    The process-keyed query HMAC lets an operator join repeated filter decisions
    without making low-entropy conversation text dictionary-reversible from
    telemetry. Paths are reduced to basenames so the sample records what was
    surfaced without disclosing project-directory structure.
    """
    logger.info(
        "[{label}] {body}",
        label=_LABEL,
        body=f"{len(candidates)} candidate(s) -> {len(picked)} kept",
        event="recall_filter",
        query_hmac_sha256=hmac.new(
            _RECALL_LOG_HMAC_KEY, query.encode(), hashlib.sha256
        ).hexdigest(),
        picked_paths=[
            path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            for path in picked[:_PICKED_PATH_SAMPLE_LIMIT]
        ],
    )


async def filter_candidates(query: str, candidates: list[Candidate]) -> list[str]:
    """The paths worth injecting, in the model's order, at most `inject_k`.

    Falls back to the first `inject_k` candidates — retrieval order, the
    behaviour before this filter — only when the filter is switched off.
    Returns `[]` when the call fails or the model judged nothing relevant:
    a filter that cannot judge must not smuggle in the unfiltered top-k it
    exists to reject.
    """
    inject_k = turn_settings.agent.memory_recall_inject_k
    if not turn_settings.agent.memory_recall_filter_enabled or not candidates:
        return [c.path for c in candidates[:inject_k]]

    import asyncio

    from shared.lm._effort import ReasoningEffort
    from shared.lm.billing import emit_billing_from_message
    from shared.lm.factory import build_chat_model

    prompt = _INSTRUCTION.format(inject_k=inject_k, query=query, candidates=_render(candidates))
    # A judge that cannot answer must not fail the turn: LLM replies are
    # statistically flaky (unparseable output, transient provider errors), so
    # the call is retried a bounded number of times and only a full exhaustion
    # is worth a warning — a single flake is routine, three in a row is not
    # (user ruling 2026-08-05: retry x3, warn only when all attempts fail).
    model = build_chat_model(
        turn_settings.agent.memory_recall_filter_model, reasoning_effort=ReasoningEffort.NONE
    )
    last_failure: str | None = None
    for _attempt in range(1, turn_settings.agent.memory_recall_filter_max_retries + 1):
        try:
            # The filter is a latency-critical background path judging names
            # and one-line descriptions only, so its model is built with
            # reasoning pinned off — "none" maps onto deepseek's thinking
            # switch, and on any other provider the effort is a no-op the
            # registry default governs. Pinning is not optional: the registry
            # defaults deepseek models to effort=max (shared/lm/registry.py),
            # which made a filter call take ~80s against the 20s bound here —
            # every call timed out and recall silently injected the unfiltered
            # top-3 the filter exists to reject.
            reply = await asyncio.wait_for(
                model.ainvoke([HumanMessage(content=prompt)]),
                timeout=turn_settings.agent.memory_recall_filter_timeout_seconds,
            )
            emit_billing_from_message(
                reply,
                model=turn_settings.agent.memory_recall_filter_model,
                usage_kind="chat",
            )
            # `.text` is a property on current langchain messages and a method
            # on older ones. Read it first and only call what is not already a
            # string, so the current path never goes through the deprecated
            # method call.
            raw_text = getattr(reply, "text", None)
            if isinstance(raw_text, str):
                text = raw_text
            elif callable(raw_text):
                text = str(raw_text())
            else:
                text = str(reply.content)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            picked = _parse(text, {c.path for c in candidates})
            if picked is not None:
                kept = picked[:inject_k]
                _log_filter_decision(query, candidates, kept)
                return kept
            last_failure = "unparseable reply"
        except Exception:
            last_failure = "filter call failed"
    logger.warning(
        "[{label}] {body}",
        label=_LABEL,
        body=f"all {turn_settings.agent.memory_recall_filter_max_retries} attempts failed ({last_failure}), injecting nothing",
        event="recall_filter",
    )
    return []
