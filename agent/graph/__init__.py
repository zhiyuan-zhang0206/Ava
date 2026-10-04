"""LangGraph definition and node implementations (async).

Submodules split by node (one file per node); leading underscore marks
package-private, no underscore marks a module promoted to a real public door
(other packages import it directly — plugin registration points, the exec
child's protocol, etc.). Three node families are subpackages, one package per
node (`claim/`, `llm/`, `exec/`) — each package door (`__init__.py`) is
docstring-only, so `claim_node` / `llm_node` / `exec_node` are re-exported from
their `node.py`. Two more packages group what the nodes read: `prompt/` (system prompt
and standing context notes) and `recall/` (passive memory recall):

  - `claim/node.py`      — claim node: pipeline orchestrator (long await + dispatch by inbound kind)
  - `claim/_batch.py`    — claim batch acquisition: idle wait loop, trim, chat deferral
  - `claim/_routing.py`  — claim lifecycle routing: ClaimGoto + batch winner resolution
  - `claim/_dispatch.py` — claim per-kind dispatch: batch state, markers, handlers
  - `claim/_decide.py`   — claim post-dispatch decision → single Command
  - `claim/_present.py`  — claim display: SSE publishing for the frontend timeline
  - `claim/_chat_inbound.py` — claim chat inbound: row → HumanMessage assembly
  - `llm/node.py`        — llm node (stream + cancel = discard partial turn)
  - `llm/_stream.py`     — llm streaming consumption (stall timeouts, non-stream fallback, cache retry)
  - `llm/_cancel.py`     — llm streaming-vs-cancel race (partial turn discard)
  - `llm/_chunk.py`      — llm chunk assembly + final-message validation
  - `llm_errors.py`      — llm stream error taxonomy + failure ledger
  - `prompt/_base_prompt.py` — immutable base system prompt + lazily captured `ava` SDK overview
  - `exec/node.py`      — exec node (one disposable subprocess per execute_code call)
  - `exec/output.py`    — code execution output envelope: format / truncate / overflow-to-file
  - `exec/protocol.py`  — exec child request/result envelopes (shared with `agent/exec_child.py`)
  - `exec/_*.py`        — exec subprocess machinery: spawn, process domain, stream, result, alerts
  - `_build.py`        — build_graph: assemble 8-Node self-cycling topology
  - `node_log.py`      — node enter/exit lifecycle log + publish timeline snapshot
  - `prompt/system_prompt.py` — system prompt dynamic assembly (base + plugin contributions)
  - `prompt/capabilities.py`, `prompt/context_notes.py` — the `# Capabilities` index, standing context notes
  - `recall/memory_recall.py` — passive memory recall (filter in `recall/_memory_filter.py`)

Public API is lazily re-exported via this __init__.py (PEP 562) — external
`from agent.graph import X` callers don't need to know the submodule layout,
and importing a light submodule (`exec.protocol`, `agent_traceback`) no
longer drags the node set into the importer — the exec child imports those
before user code runs (startup-path laziness, task #3585).
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Real signatures for static checkers: `__getattr__` alone types the names
    # `Any`, which degrades callers' narrowing (an `isinstance(x, Command)` check
    # narrowing to `Command[Unknown]` under the strict tests/agent rules). Runtime
    # stays lazy — the node set is a heavy import on paths (the exec child) that
    # never use these names (agent/graph/__init__.py in `_TYPE_CHECKING_ALLOWED`).
    from ._build import build_graph as build_graph
    from .claim.node import claim_node as claim_node
    from .exec.node import exec_node as exec_node
    from .exec.output import EXEC_CANCEL_NOTE as EXEC_CANCEL_NOTE
    from .llm.node import llm_node as llm_node
    from .llm_errors import LlmLedger as LlmLedger

# Eager `from ._build import build_graph` used to run on every `agent.graph`
# import — pulling the full node set (build/claim/llm/exec and their trees)
# into every shell/files exec child. Resolve on first attribute access instead.
_LAZY_EXPORTS = {
    "build_graph": "._build",
    "claim_node": ".claim.node",
    "exec_node": ".exec.node",
    "EXEC_CANCEL_NOTE": ".exec.output",
    "llm_node": ".llm.node",
    "LlmLedger": ".llm_errors",
}

# Static checkers see the real signatures through the TYPE_CHECKING block
# above; at runtime `__getattr__` resolves the names lazily (PEP 562).
__all__ = [
    "EXEC_CANCEL_NOTE",
    "LlmLedger",
    "build_graph",
    "claim_node",
    "exec_node",
    "llm_node",
]

# False until this module finishes importing (set True at the very bottom).
# Attribute lookups during that window must fall through to the standard
# submodule machinery — the same re-entrancy guard as `ava/__init__.py`'s
# `_init_complete` latch, and the inert posture if a future edit re-adds a
# submodule import to this package's body.
_init_complete = False


def __getattr__(name: str) -> Any:
    """PEP 562 lazy re-export of the submodule API (see `_LAZY_EXPORTS`)."""
    if _init_complete and name in _LAZY_EXPORTS:
        return getattr(importlib.import_module(_LAZY_EXPORTS[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_init_complete = True
