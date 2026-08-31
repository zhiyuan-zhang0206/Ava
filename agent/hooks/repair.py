"""Built-in repair hook: restore valid tool_use/tool_result pairing after a crash.

A hard cancel (SIGTERM / restart / stop kill / OOM -> asyncio.CancelledError)
can kill the process after the AIMessage(tool_use) was committed but before
exec_node wrote the paired tool_result. Resuming from that checkpoint sends an
API-invalid message list (tool_use with no following tool_result), which
Anthropic-compat providers reject with 400 on every turn (agent 167 2026-06-06;
agents 236/238 2026-07-13).

The converse crash shape happened to agent 5333 (2026-08-31): a ToolMessage
tool_result reached the checkpoint without its carrying AIMessage(tool_use).
LangChain normalization strips a synthetic replacement tool_use from the wire
payload, so synthesizing the missing pair leaves the request invalid. Drop the
orphan result instead: it carries no model-visible request, so its removal loses
nothing the model can honestly observe; the interrupted turn is absent.

Two repair passes share `dangling_tool_pairing_repairs`:

- **Boot pass** (`agent/startup.py:_repair_dangling_tool_use_at_startup`, runs
  before the first ainvoke): catches dangling committed to the checkpoint
  before claim appends anything — including before a pending compact_request
  runs its Compaction LLM call over the history in the claim node.
- **before_llm hook** (registered here, runs after claim on every turn):
  catches what the boot pass structurally cannot see. When the shutdown's
  consolidating checkpoint `aput` fails, the dangling AIMessage survives only
  in `checkpoint_writes` (a pending write) — invisible to `aget_state` at
  boot, materialized by pregel only when the graph resumes. By then claim has
  appended restart_completed / resurrect / reminder inbounds, burying the
  dangling tool_use mid-history (the 2026-07-13 shape: wedged 236/238 in a
  permanent per-turn 400 retry loop for 18h).

The synthetic tool_result records the interruption truthfully rather than
masking a model error: the model never produced bad output, the process was
killed mid-tool, so this does not violate fail-fast.

Shape contract: detection treats an unpaired tool_call as a crash artifact,
because every live path preserves pairing — exec always writes a ToolMessage,
syntax_fix supplies its own ToolMessage when it skips exec, and the llm node's
silent-idle branches bypass exec only when tool_calls is empty. A future hook
that intentionally withholds a tool_result would be reinterpreted as a crash
and handed a synthetic [interrupted] result — keep the pairing invariant when
extending the hook surface.

Every repair returns one REMOVE_ALL_MESSAGES rebuild: the original AIMessage
and any synthetic [interrupted] ToolMessage therefore share one messages-channel
value and one checkpoint blob. A bare tail append can be committed separately
from its tool_use during kill/n-step-save interleavings, recreating the 5333
orphan shape this repair removes.

Residual gap, accepted: a force-compact whose threshold trips on the same turn
that materializes a pending-write dangling runs its summarization LLM call
(`compact.py:_force_compact`) over the pre-repair state and 400s — hooks in one
pass all see the same pre-hook state. That needs hard-cancel mid-exec + stranded
pending write + context crossing the force threshold on the same turn; the next
turn's claim path is guarded by the boot-pass-repaired checkpoint shape.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.messages.modifier import RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from agent import state as _state
from agent.hooks._registry import Hook
from agent.messages import exec_output_message
from shared.context import AvaContext, agent_id_from_config
from shared.log import logger

_INTERRUPTED_TOOL_RESULT = (
    "[interrupted: the agent process was cancelled before this tool produced a result]"
)


def _unpaired_tool_calls(messages: Sequence[AnyMessage]) -> list[tuple[int, list[str]]]:
    """(index, unpaired tool_call ids) for every AIMessage whose tool_calls
    lack a ToolMessage in the immediately following run of ToolMessages."""
    out: list[tuple[int, list[str]]] = []
    for i, m in enumerate(messages):
        if not isinstance(m, AIMessage):
            continue
        tool_calls = getattr(m, "tool_calls", None) or []
        if not tool_calls:
            continue
        unpaired = {tc["id"] or "" for tc in tool_calls}
        for nxt in messages[i + 1 :]:
            if not isinstance(nxt, ToolMessage):
                break
            unpaired.discard(nxt.tool_call_id)
        if unpaired:
            # Preserve tool_calls order, not set-iteration order.
            out.append((i, [tc["id"] or "" for tc in tool_calls if (tc["id"] or "") in unpaired]))
    return out


def _orphan_tool_results(messages: Sequence[AnyMessage]) -> list[int]:
    """Indices of ToolMessages lacking a tool_use in any preceding AIMessage."""
    preceding_tool_call_ids: set[str] = set()
    out: list[int] = []
    for i, message in enumerate(messages):
        if isinstance(message, AIMessage):
            for tool_call in message.tool_calls:
                tool_call_id = tool_call["id"] or ""
                preceding_tool_call_ids.add(tool_call_id)
        elif (
            isinstance(message, ToolMessage) and message.tool_call_id not in preceding_tool_call_ids
        ):
            out.append(i)
    return out


def _synthetic_tool_result(tool_call_id: str) -> ToolMessage:
    return exec_output_message(
        content=_INTERRUPTED_TOOL_RESULT,
        tool_call_id=tool_call_id,
        exit_code=-1,
        cancelled=True,
        created_at=datetime.now(UTC),
    )


def dangling_tool_pairing_repairs(messages: Sequence[AnyMessage]) -> list[Any]:
    """Return one full rebuild repairing both dangling pairing directions.

    Orphan tool_results are dropped first. Every remaining dangling tool_use
    receives a synthetic [interrupted] result immediately after its trailing
    ToolMessage run. `[]` means the complete history is API-valid.
    """
    orphan_indices = set(_orphan_tool_results(messages))
    survivors = [message for i, message in enumerate(messages) if i not in orphan_indices]
    dangling = _unpaired_tool_calls(survivors)
    if not orphan_indices and not dangling:
        return []
    # Insertion point per dangling AIMessage: after the trailing run of
    # ToolMessages already pairing some of its tool_calls.
    inserts: dict[int, list[ToolMessage]] = {}
    for i, ids in dangling:
        j = i + 1
        while j < len(survivors) and isinstance(survivors[j], ToolMessage):
            j += 1
        inserts[j - 1] = [_synthetic_tool_result(t) for t in ids]
    rebuilt: list[Any] = [RemoveMessage(id=REMOVE_ALL_MESSAGES)]
    for k, m in enumerate(survivors):
        rebuilt.append(m)
        rebuilt.extend(inserts.get(k, ()))
    return rebuilt


class _RepairDanglingToolPairingHook(Hook):
    """Built-in before_llm hook repairing dangling tool_use/tool_result pairs.

    See the module docstring for the crash shapes and the drop-versus-synthesize
    verdict.
    """

    async def __call__(
        self,
        state: _state.AgentState,
        _runtime: Runtime[AvaContext],
        config: RunnableConfig,
        /,
    ) -> dict | None:
        repairs = dangling_tool_pairing_repairs(state.messages)
        if not repairs:
            return None
        logger.warning(
            "repaired dangling tool_use/tool_result pairing in materialized state before LLM call",
            event="dangling_tool_pairing_repaired",
            agent_id=agent_id_from_config(config),
        )
        return {"messages": repairs}


# Module-level singleton — the registered instance. `register_repair_hooks`
# re-appends this same object on each graph build, so its identity is stable
# (tests assert `HOOKS["before_llm"][-1] is _repair_dangling_tool_pairing`).
_repair_dangling_tool_pairing = _RepairDanglingToolPairingHook()


def register_repair_hooks() -> None:
    """Register the built-in dangling-tool-pairing repair before_llm hook.

    Called once at graph build time (from `build_graph`), before
    `register_compact_hooks` — repair guards the payload every hook after it
    may feed to an LLM. Unconditional: crash recovery is core, not a plugin.
    """
    from agent.hooks import register_before_llm

    register_before_llm(_repair_dangling_tool_pairing)
