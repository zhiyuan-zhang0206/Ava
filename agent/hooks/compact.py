"""Built-in compact hook: auto-compact when context exceeds threshold + Compaction
LLM helper.

Exhausted compaction attempts raise `CompactionFailedError`; the hosted turn
boundary reports the failure, preserves history, and durably halts until new
inbound work arrives. A consumed user compaction request is not replayed.

Compaction is a core capability (Issue #1284) — this module is always active,
not gated behind a plugin. The before_llm hook is registered by
`register_compact_hooks()`, called from `build_graph()`.

Exports:
- `generate_summary(messages, llm) -> summary`: pure function that runs the
  Compaction LLM over the whole conversation and returns the summary text.
- `register_compact_hooks()`: registers the before_llm hook (force-compact +
  reminder). Called once at graph build time.

Redesign plan for forced / command / spontaneous compact:
`agent/compaction-redesign.md`.

Compaction replaces the whole history with `[system prompt, summary]` — the
summary is the complete memory, nothing raw is carried over. No tail of recent
messages is appended; recency that matters is captured *inside* the summary.

The compaction request is shaped to ride the backend's automatic prefix cache:
it reuses the conversation exactly as the main llm node already sent it — same
leading SystemMessage, same message objects, same single bound tool — and
appends one instruction message. See:
decisions/2026-04-18-in-place-compact.md.

The system-prompt snapshot invariant: messages[0] is built by
build_system_prompt() exactly once, on the agent's first round, and never
rebuilt — stable across restarts, upgrades, and config changes precisely so
the cached prefix survives them.
"""

__description__ = "Auto-compact history when token count exceeds threshold"

from datetime import UTC, datetime
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_core.messages.modifier import RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.history_dump import dump_history, history_dump_note
from agent.hooks import Hook, register_before_llm
from agent.lm_cache import ainvoke_with_cache_retry
from agent.messages import (
    COMPACT_SUMMARY_HEADER,
    NoteTag,
    system_note_message,
    tail_has_agent_inbound,
)
from agent.nodes import INIT_CONTEXT, LLM
from agent.state import AgentState, CompactState, ContextReset
from shared.audit_events import insert_event_log_async
from shared.checkpoint_cleanup import mark_compact_boundary, trim_checkpoints
from shared.config.turn_view import turn_settings
from shared.context import AvaContext, agent_id_from_config
from shared.live_events import CompactDone
from shared.lm.context_budget import latest_input_tokens, resolve_context_budget
from shared.log import logger
from shared.message_kwargs import AvaMsgType, read_ava_kwargs

# Compaction bookkeeping lives in the nested `compact` sub-state (CompactState)
# on BaseAgentState — read via `state.compact.version` etc.; writers overwrite
# the whole `compact` channel via `state.compact.model_copy(update=...)`.

# The one compaction contract — what a summary must contain and how to write
# it — lives in one place: the `ava.self.compact` docstring, which the agent
# reads in its own SDK and which is also part of this request's leading prompt
# (the SDK reference renders `self`). This instruction therefore does not
# restate the section template; it only frames the moment, the few rules
# specific to a model writing the summary here, and points at that contract.
COMPACTION_INSTRUCTION = """[system] Context compaction checkpoint: the conversation above is about to be
replaced by the summary you write now. Produce it now as plain text — do not
write code or call any tool; anything other than text is discarded. Write it
exactly as the `ava.self.compact` contract above specifies: first person, every
section filled, "(none)" only when one genuinely has nothing. Refer to others as
"the user" or "agent N". If a summary already appears above, rewrite it in place
— one flat, updated summary, never a summary nested inside a summary.
"""


def compose_summary_message(summary: str) -> str:
    """The header + the summary, as the single text injected on the agent's
    behalf when its context is replaced. Shared by every compact path so the
    framing is identical across forced / command / spontaneous compaction.
    The header itself (with the rationale for its wording) lives in
    `agent/messages.py:COMPACT_SUMMARY_HEADER` — the read-side classifier
    (gateway/context_breakdown.py) keys on it too."""
    return f"{COMPACT_SUMMARY_HEADER}\n\n{summary}"


# Compaction summary monitoring + retry knobs.
COMPACT_MIN_SUMMARY_CHARS = 1000
COMPACT_MAX_ATTEMPTS = 3

# The no-LLM fallback summary of the overflow emergency path
# (emergency_compact_summary): produced when the provider rejects even the
# compaction request itself (context over the effective input ceiling), so the
# agent is rescued without any model call. The text is the fresh context's
# whole memory — it names where the dropped history went (the history_dump
# note rides the tail) and where the agent's durable state lives.
_EMERGENCY_COMPACT_MARKER = (
    "[system] Emergency context trim: the conversation was removed because the "
    "context window overflowed and the provider also rejected the compaction "
    "call itself (the request no longer fits). Your personal memory "
    "(memory/ + MEMORY.md) and workspace files are intact — read them to "
    "reconstruct where you were. If a summary was preserved from an earlier "
    "compaction it follows below."
)


class CompactionFailedError(RuntimeError):
    """Compaction could not produce a usable summary after retries.

    Raised by both compaction paths (the before_llm auto-compact hook and the
    claim node's compact_request arm) once every attempt failed — the model
    ignored the template, or the provider kept failing. It is the framework
    signal the runloop turns into a turn-abort (agent stays alive, idles for
    the next inbound) instead of an unhandled exception that kills the process
    into a non-resurrectable 'exit' termination. Subclasses RuntimeError so
    pre-existing `except RuntimeError` handling keeps working.
    """


# How many checkpoints to keep when a compaction trims the now-frozen
# per-turn history. A compaction replaces the whole conversation, so the
# pre-compact checkpoints will never be resumed again — only the latest
# (holding the full pre-compact history) matters: every channel stores a
# full snapshot, so the newest checkpoint alone restores the complete state
# (the safety invariant documented in shared/checkpoint_cleanup.py). The raw
# pre-compact history's only durable home is the summary itself (plus the
# events mirror); older checkpoints would only retain redundant full copies
# of the same conversation (2026-08-10, Task #1125 ruling).
COMPACT_TRIM_KEEP = 1


async def trim_checkpoints_after_compact(pool: AsyncConnectionPool | None, agent_id: int) -> None:
    """Stamp the compaction boundary, then trim the pre-compact history to keep=1.

    Best-effort, in two steps: first `mark_compact_boundary` stamps the newest
    pre-compact checkpoint (the full-snapshot record of this compaction segment)
    for timeline segment reads while retained. The keep=1 trim drops the rest
    of the now-frozen pre-compact history; the stamped survivor holds the
    complete segment, but is not prune-exempt once it ages beyond a later keep
    window. Storage cleanup must never abort the agent's turn (it runs inside
    the graph), so a failure is logged and swallowed — the next trim trigger
    retries. `pool is None` (container / eval mode) is a no-op. Shared by the
    auto-compact hook here and the agent-/user-triggered compact paths in the
    claim node.
    """
    if pool is None:
        return
    try:
        await mark_compact_boundary(pool, str(agent_id))
        await trim_checkpoints(pool, str(agent_id), keep=COMPACT_TRIM_KEEP)
    except Exception as exc:
        logger.warning(
            "[{label}] {body}",
            label="checkpoint-trim",
            event="checkpoint_trim",
            body=f"compact trim failed for agent {agent_id}: {exc!r}",
        )


def conversation_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Strip the leading SystemMessage (Ava's SYSTEM_PROMPT snapshot) — it is
    the prompt, not conversation history, and is not part of what gets
    summarized."""
    has_system = bool(messages) and isinstance(messages[0], SystemMessage)
    return messages[1:] if has_system else messages


def emit_compaction_monitoring(
    messages: list[AnyMessage], summary: str, *, agent_id: int, compact_kind: str
) -> None:
    """Record one applied compaction's size reduction and completed count.

    Requests and agent-authored summaries are audit events, but neither proves
    that the history replacement happened. This event is emitted only by the
    two paths that apply the replacement, so its counter is compaction
    frequency. The ratio intentionally excludes Ava's standing system prompt:
    it is rebuilt after compaction and is not part of the discarded history.
    """
    history_chars = sum(
        len(message.content) if isinstance(message.content, str) else len(str(message.content))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        for message in conversation_messages(messages)
    )
    summary_chars = len(summary)
    summary_history_ratio = summary_chars / history_chars if history_chars else None
    logger.info(
        "compaction completed: {history_chars} history chars -> {summary_chars} summary chars",
        event="compaction_completed",
        agent_id=agent_id,
        compact_kind=compact_kind,
        compactions=1,
        history_chars=history_chars,
        summary_chars=summary_chars,
        summary_history_ratio=summary_history_ratio,
    )


async def generate_summary(
    messages: list[AnyMessage],
    llm: BaseChatModel,
) -> str:
    """Run the Compaction LLM over the whole conversation; returns the summary text.

    Used by the claim node for handling inbound kind `compact_request`
    (backend LLM generation); `compact_summary` (agent-written) skips this
    function. Also used by this module's auto-compact hook.

    The request = the conversation exactly as the main llm node sends it
    (same `prepare_invocation` shape — explicit Gemini cache when live,
    otherwise leading SystemMessage included + `execute_code` bound) + one
    trailing instruction message. The whole conversation is summarized — the
    model sees every message, including the most recent, and the summary it
    returns is the complete replacement memory (no raw tail is kept beside
    it). The request is the previous turn's request plus one message, so it
    still hits the backend prefix cache.

    Raises:
        ValueError: the conversation is empty — nothing to summarize.
        RuntimeError: the Compaction LLM returned no text (e.g. it disobeyed
            the instruction and only emitted a tool call) — there is no
            summary to apply.
    """
    has_system = bool(messages) and isinstance(messages[0], SystemMessage)
    system_head = messages[:1] if has_system else []
    content_msgs = messages[1:] if has_system else messages

    if not content_msgs:
        raise ValueError("conversation is empty, nothing to compress")

    compaction_input = [
        *system_head,
        *content_msgs,
        HumanMessage(content=COMPACTION_INSTRUCTION),
    ]
    # Same request shape as the llm node via prepare_invocation: when a
    # Gemini explicit cache is live the summary call rides it too (and its
    # stale-retry recovers a lapsed TTL), otherwise plain bind_tools.
    response = await ainvoke_with_cache_retry(llm, compaction_input)
    model = getattr(llm, "model_name", None) or turn_settings.lm.llm_model
    if isinstance(model, str) and model:
        from shared.lm.usage import log_usage_from_message

        log_usage_from_message(response, model=model, usage_kind="agent")
    summary = response.text
    if not summary.strip():
        raise RuntimeError(
            f"Compaction LLM returned no text content"
            f" (summarizing {len(content_msgs)} messages,"
            f" response content type {type(response.content).__name__});"  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            f" cannot replace history with an empty summary"
        )
    return summary


def _last_compact_summary_text(messages: list[AnyMessage]) -> str | None:
    """The most recent prior compaction's summary text still in the
    conversation, or None.

    A compaction replaces the whole history with `[system, summary]` and the
    following conversation grows after it, so at overflow time the last
    preserved summary sits at messages[1] — a HumanMessage stamped
    `ava_msg_type` compact_summary / compact_request. The emergency fallback
    embeds it verbatim so a forced trim keeps as much memory as a model-less
    wipe can (the summary is the compact contract's "complete memory" of
    everything before it).
    """
    for msg in messages[1:]:
        kw = read_ava_kwargs(msg)
        if kw.get("ava_msg_type") in (
            AvaMsgType.COMPACT_SUMMARY.value,
            AvaMsgType.COMPACT_REQUEST.value,
        ):
            content = msg.content  # pyright: ignore[reportUnknownMemberType]
            if isinstance(content, str) and content:
                return content
            return None
    return None


def _is_permanent_provider_failure(exc: BaseException) -> bool:
    """Whether ``exc`` is a PERMANENT-class provider rejection (the compaction
    request itself cannot go out — context over the effective ceiling). Any
    other failure (transient network / provider 5xx / empty-model-output) must
    NOT trigger the wipe fallback: it would destroy the conversation for a
    blip that a retry or a later attempt clears."""
    from shared.lm.errors import ErrorClass, classify_error

    return classify_error(exc).error_class is ErrorClass.PERMANENT


def _emergency_fallback_summary(messages: list[AnyMessage]) -> str:
    """The no-LLM fallback summary: the emergency marker plus the last
    preserved prior summary, if one exists. Never raises, never calls the
    model — the whole point is to rescue an agent whose context the provider
    rejects outright.

    Deliberately exempt from `COMPACT_MIN_SUMMARY_CHARS` (the marker alone is
    ~400 chars): that gate exists to reject a MODEL that ignored the summary
    template, whereas this text is framework-authored and always follows the
    format — there is nothing to detect, and rejecting it would strand the
    agent in the overflow state this path exists to end. Downstream
    (`build_compact_transition`) never checks the length either."""
    preserved = _last_compact_summary_text(messages)
    if preserved:
        return f"{_EMERGENCY_COMPACT_MARKER}\n\n{preserved}"
    return _EMERGENCY_COMPACT_MARKER


async def emergency_compact_summary(messages: list[AnyMessage], llm: BaseChatModel) -> str:
    """The circuit-breaker compaction summary: a real compaction first, then the
    no-LLM fallback — used by the overflow self-rescue path (claim decide).

    The breaker opens on a permanent context-overflow rejection, so the
    conversation is already over the provider's effective input ceiling; the
    compaction request (the same conversation plus one instruction) may be
    rejected too. This mirrors `generate_summary`'s retry loop, with two
    differences:

    - a PERMANENT-class failure stops retrying immediately and returns the
      minimal fallback (marker + last preserved summary) instead of raising —
      a request the provider refuses outright cannot be fixed by retry, and
      the wipe must still happen for the agent to recover;
    - a transient failure / short summary retries, and raising
      `CompactionFailedError` when exhausted stays the outcome — a provider
      blip or a template-defying model must not silently destroy the
      conversation either.

    Returns the summary text; callers feed it through the normal compact
    transition (build_compact_transition), so the fallback is indistinguishable
    from a real compaction downstream (same header, same wipe, same notes).
    """
    last_error: Exception | None = None
    for attempt in range(1, COMPACT_MAX_ATTEMPTS + 1):
        try:
            summary = await generate_summary(messages, llm)
        except Exception as e:
            last_error = e
            if _is_permanent_provider_failure(e):
                logger.warning(
                    "[{label}] {body}",
                    label="emergency-compact",
                    event="emergency_compact",
                    body=(
                        f"attempt {attempt}/{COMPACT_MAX_ATTEMPTS}: compaction call "
                        f"rejected ({e!r}) — falling back to the no-LLM minimal compact"
                    ),
                )
                return _emergency_fallback_summary(messages)
            logger.warning(
                "[{label}] {body}",
                label="emergency-compact",
                event="emergency_compact",
                body=f"attempt {attempt}/{COMPACT_MAX_ATTEMPTS}: {e}; retrying",
            )
            continue
        if len(summary) >= COMPACT_MIN_SUMMARY_CHARS:
            logger.info(
                "[{label}] {body}",
                label="emergency-compact",
                event="emergency_compact",
                body=f"attempt {attempt}/{COMPACT_MAX_ATTEMPTS}: summary {len(summary)} chars",
            )
            return summary
        last_error = None
        logger.warning(
            "[{label}] {body}",
            label="emergency-compact",
            event="emergency_compact",
            body=f"attempt {attempt}/{COMPACT_MAX_ATTEMPTS}: summary too short; retrying",
        )
    raise CompactionFailedError(
        f"Emergency compaction produced no usable summary across {COMPACT_MAX_ATTEMPTS}"
        f" attempts (last: {last_error!r}) — refusing to wipe the conversation with a"
        f" non-summary; the turn is aborted and the breaker stays open for the next wake"
    ) from last_error


def _estimate_tokens(messages: list[AnyMessage]) -> int:
    """Simple estimate: total content chars / 4. Used only as the turn-1
    fallback for context occupancy — before the first LLM call completes there
    is no real ``input_tokens`` to read (see ``_context_occupancy``)."""
    total_chars = sum(
        len(m.content) if isinstance(m.content, str) else len(str(m.content))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        for m in messages
    )
    return total_chars // 4


def _context_occupancy(messages: list[AnyMessage]) -> int:
    """Context-window occupancy, in the same unit the frontend gauge shows: the
    most recent LLM call's real ``input_tokens`` (provider truth). Falls back to
    the chars/4 estimate only until the first call completes (a just-spawned
    agent's first turn, or right after a compaction wiped the prior AIMessages),
    when the context is trivially small anyway.

    This is the "Option Y" approach: the compact trigger, the reported soft/hard
    thresholds, and the gauge are all one unit. The trade-off is that occupancy
    now reflects the *previous* call's measured size rather than a fresh estimate
    of the context about to be sent — so compaction fires one turn later than the
    old chars/4 gate, absorbed by the completion buffer the fractions leave."""
    tokens = latest_input_tokens(messages)
    return tokens if tokens is not None else _estimate_tokens(messages)


def auto_compact_will_fire(state: AgentState) -> bool:
    """Whether the force-compact path would replace ``state.messages`` this turn:
    occupancy over the model's hard ceiling AND a non-empty conversation to
    compress. The single gate — plugins that must defer a message write on a
    turn compaction will claim (agent-reply / memory / silent-idle notes) call
    this instead of replicating the estimate + threshold, so the prediction can
    never drift from ``auto_compact_before_llm``.

    Resolves the ceiling from the agent's own model (``turn_settings.lm.llm_model``,
    which the spawn overlay already applied); ``UnknownModelWindowError`` surfaces
    rather than silently mis-gating an agent whose window we do not know."""
    budget = resolve_context_budget(turn_settings.lm.llm_model)
    if _context_occupancy(state.messages) <= budget.hard_compact_tokens:
        return False
    return bool(conversation_messages(state.messages))


async def auto_compact_before_llm(
    state: AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
) -> dict | None:
    """Before each LLM call, estimate state.messages token count and compact
    if it exceeds the threshold.

    The compaction gate below (occupancy over the model's hard ceiling AND a
    non-empty conversation to compress) is exposed as `auto_compact_will_fire`
    for plugins that must defer a message write on a turn this fires — the one
    gate, no replicated estimate to drift.

    The summary itself is generated by `generate_summary`, whose
    COMPACTION_INSTRUCTION template is the actual quality defense. The only
    extra safety here is a retry: a summary shorter than
    COMPACT_MIN_SUMMARY_CHARS means the model ignored the template (the
    agent-240 incident), so the cache-mostly request is retried; if every
    attempt stays short (or returns no text), this raises rather than overwrite
    history with a non-summary — a real instruction-following regression that
    must surface, not a state to paper over.
    """
    occupancy = _context_occupancy(state.messages)
    if occupancy <= resolve_context_budget(turn_settings.lm.llm_model).hard_compact_tokens:
        return None
    content_msgs = conversation_messages(state.messages)
    if not content_msgs:
        logger.info(
            "[{label}] {body}",
            label="auto-compact",
            event="auto_compact",
            body=f"skip: nothing to compress (no conversation messages), tokens≈{occupancy}",
        )
        return None
    llm = runtime.context.llm
    assert llm is not None, "auto_compact_before_llm requires ctx.llm"  # noqa: S101
    logger.info(
        "[{label}] {body}",
        label="auto-compact",
        event="auto_compact",
        body=f"start: tokens≈{occupancy}, compressing {len(content_msgs)} messages",
    )

    summary: str = ""
    last_error: Exception | None = None
    for attempt in range(1, COMPACT_MAX_ATTEMPTS + 1):
        try:
            summary = await generate_summary(state.messages, llm)
        except Exception as e:
            last_error = e
            logger.warning(
                "[{label}] {body}",
                label="auto-compact",
                event="auto_compact",
                body=f"attempt {attempt}/{COMPACT_MAX_ATTEMPTS}: {e}; retrying",
            )
            continue
        logger.info(
            "[{label}] {body}",
            label="auto-compact",
            event="auto_compact",
            body=f"attempt {attempt}/{COMPACT_MAX_ATTEMPTS}: summary {len(summary)} chars",
        )
        if len(summary) >= COMPACT_MIN_SUMMARY_CHARS:
            break
        last_error = None
    else:
        detail = f"error: {last_error}" if last_error else f"{len(summary)} chars"
        # CompactionFailedError (not a bare RuntimeError) so the runloop can
        # turn it into a turn-abort instead of process death: the agent stays
        # alive and the error is visible on the timeline (2026-08-08 audit,
        # P1-1 — a compact failure used to kill the process into a
        # non-resurrectable 'exit', one crash per incoming message while the
        # cause persisted).
        raise CompactionFailedError(
            f"Compaction produced no usable summary across {COMPACT_MAX_ATTEMPTS}"
            f" attempts (last: {detail}); the model is not following the"
            f" compaction template — refusing to overwrite {len(content_msgs)} messages"
            f" with a non-summary"
        ) from last_error

    logger.info(
        "[{label}] {body}",
        label="auto-compact",
        event="auto_compact",
        body=f"done: summary {len(summary)} chars",
    )

    agent_id = agent_id_from_config(config)
    emit_compaction_monitoring(
        state.messages,
        summary,
        agent_id=agent_id,
        compact_kind="auto",
    )
    pool = runtime.context.ops_pool

    # Record the compact in event_log (best-effort: a failure only loses the
    # audit row, never the summary itself).
    if pool is not None:
        try:
            await insert_event_log_async(
                event_type="compact",
                agent_id=agent_id,
                source="system",
                payload={"compact_kind": "auto", "length": len(summary)},
            )
        except Exception as exc:
            logger.warning(
                "[{label}] {body}",
                label="auto-compact",
                event="auto_compact",
                body=f"failed to insert event_log: {exc!r}",
            )

    publisher = runtime.context.event_publisher
    if publisher is not None:
        publisher.emit(CompactDone(agent_id=agent_id).model_dump_json())

    # REMOVE_ALL wipes the whole window, standing head included. Re-establishing
    # it is `init_context`'s job: clear the history, park the summary as what
    # follows the head, and detour there. The turn resumes at the LLM with the
    # short context, which is where this hook was headed anyway.
    #
    # The summary message carries the same ava_msg_type stamp as the claim-node
    # compact path (Task #1017): without it the timeline read side classifies
    # the HumanMessage as a catch-all system_marker with source=null and the
    # frontend renders the red "UNRECOGNIZED SYSTEM_MARKER" alarm. Force and
    # auto compaction must produce identical message contracts.
    #
    # Opt-in forensics: snapshot the full pre-compact conversation to the agent
    # workspace and point the fresh context at it. The note rides the parked
    # tail (after the summary), never the live channel — the wipe is a clean
    # REMOVE_ALL, and a note between an AIMessage and its ToolMessage would be
    # rejected by the provider. Best-effort: a dump failure must never abort
    # the compaction itself.
    dump_path = dump_history(state.messages, agent_id)
    return build_compact_transition(
        summary,
        resume=LLM,
        extra_msgs=([history_dump_note(dump_path)] if dump_path is not None else None),
        summary_kwargs={
            "additional_kwargs": {
                "ava_msg_type": AvaMsgType.COMPACT_SUMMARY.value,
                "ava_created_at": datetime.now(UTC).isoformat(),
            },
        },
    )


# ── Shared compact transition builder ──
# Both the claim node (agent-/user-triggered) and the auto-compact hook
# (forced) need the same ContextReset + REMOVE_ALL + INIT_CONTEXT skeleton.
# This function is the single source of truth; callers layer their own extras
# (halted, update_initiated, version bump, event publish, checkpoint trim).


def build_compact_transition(
    summary: str,
    *,
    resume: str,
    extra_msgs: list[AnyMessage] | None = None,
    summary_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared skeleton for every compact path.

    Returns a partial state update dict carrying:
    - ``messages``: REMOVE_ALL (wipes the whole window, standing head included)
    - ``context_reset``: ContextReset with the summary message and any
      framework lifecycle notes (resurrect / fork markers) following it,
      resuming at *resume* — a compact is a clean wipe: raw conversation
      messages never survive it (the claim node re-delivers chats co-batched
      with the compact as pending inbounds instead of parking them here)
    - ``goto``: INIT_CONTEXT (the detour every compact path takes)

    Callers must add ``compact`` version bump, ``halted``, ``update_initiated``,
    event publish, and checkpoint trim as their path needs.
    """
    summary_msg_kwargs = summary_kwargs or {}
    tail_msgs: list[AnyMessage] = [
        HumanMessage(
            content=compose_summary_message(summary),
            **summary_msg_kwargs,
        ),
    ]
    if extra_msgs:
        tail_msgs.extend(extra_msgs)
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)],
        "context_reset": ContextReset(
            tail=tail_msgs,
            resume=resume,  # type: ignore[arg-type]
        ),
        "goto": INIT_CONTEXT,
    }


# ── Compact reminder (moved from ava_builtins/plugins/ava_compact/plugin.py, Issue #1284) ──

COMPACT_REMINDER_NOTE = (
    "Your working context is getting long — you may want to wind it "
    "down soon. Persist anything durable to files first, then call "
    "ava.self.compact(summary), written as its docstring specifies."
)


def _compact_reminder_update(state: AgentState) -> dict | None:
    """The one-time wind-down reminder, injected when occupancy sits in the band
    below the forced ceiling (soft_compact_tokens < occupancy <=
    hard_compact_tokens). Returns the `messages` update + bookkeeping, or None.

    The caller only invokes this when the force path did NOT fire, so occupancy
    is already at-or-below the hard ceiling — force and reminder are mutually
    exclusive by threshold and never both write `messages` in one pass.

    Returns None (no reminder) when:
    - tokens are below the reminder threshold, or there is nothing to compact;
    - the turn was woken by an agent inbound: the agent-reply note claims the
      single `messages` write a before_llm pass allows;
    - the reminder already fired this context window. It re-arms when a
      compaction advances compact.version past the stored bookmark.
    """
    occupancy = _context_occupancy(state.messages)
    if occupancy <= resolve_context_budget(turn_settings.lm.llm_model).soft_compact_tokens:
        return None
    if not conversation_messages(state.messages):
        return None
    if tail_has_agent_inbound(state.messages):
        return None

    compact: CompactState = state.compact
    shown: bool = compact.reminder_shown
    seen: int = compact.reminder_seen_version
    if compact.version > seen:
        shown = False  # a compaction summarized the old reminder away -> re-arm
        seen = compact.version
    if shown:
        return None

    logger.info(
        "[{label}] {body}",
        label="compact-reminder",
        event="compact_reminder",
        body=f"injecting wind-down note, tokens≈{occupancy}",
    )
    return {
        "messages": [
            system_note_message(
                content=COMPACT_REMINDER_NOTE,
                tag=NoteTag.COMPACT_REMINDER,
                created_at=datetime.now(UTC),
            )
        ],
        # Overwrite the whole compact channel: flip the reminder flags, keep
        # version (model_copy from the current value) so the last-value channel
        # does not reset it.
        "compact": compact.model_copy(
            update={"reminder_shown": True, "reminder_seen_version": seen}
        ),
    }


class _AutoCompactHook(Hook):
    """Single before_llm hook: force-compact at the ceiling, else the one-time
    wind-down reminder in the band below it.

    Keeping both branches in one hook is what makes the clobber-safety hold:
    force (occupancy > hard ceiling) and reminder (occupancy <= hard ceiling)
    are mutually exclusive, so this hook writes `messages` at most once per pass
    — never the same-key double-write the hook runner rejects.

    On a successful force compaction, bump compact.version so subscribers
    can detect it, and trim the now-frozen per-turn checkpoints.
    """

    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[AvaContext],
        config: RunnableConfig,
        /,
    ) -> dict | None:
        result = await auto_compact_before_llm(state, runtime, config)
        if result is not None:
            await trim_checkpoints_after_compact(
                runtime.context.ops_pool, agent_id_from_config(config)
            )
            # Bump version, keep the reminder flags (model_copy from the current
            # value); result carries `messages` / `context_reset` / `goto`, so
            # `compact` is not a key collision.
            return {
                **result,
                "compact": state.compact.model_copy(update={"version": state.compact.version + 1}),
            }
        return _compact_reminder_update(state)


# Module-level singleton — the registered instance. `register_compact_hooks`
# re-appends this same object on each graph build.
_auto_compact_with_version_bump = _AutoCompactHook()


def register_compact_hooks() -> None:
    """Register the built-in compact before_llm hook.

    Called once at graph build time (from `build_graph`). Because compact is
    now a core capability (Issue #1284), this registration is unconditional
    and does not depend on any plugin enable/disable config.
    """
    register_before_llm(_auto_compact_with_version_bump)
