"""LangGraph definition and node implementations (async).

Submodules split by node (one file per node, `_` prefix marks internal impl):

  - `_claim.py`        — claim node: pipeline orchestrator (long await + dispatch by inbound kind)
  - `_claim_batch.py`  — claim batch acquisition: idle wait loop, trim, chat deferral
  - `_claim_routing.py`— claim lifecycle routing: ClaimGoto + batch winner resolution
  - `_claim_dispatch.py` — claim per-kind dispatch: batch state, markers, handlers
  - `_claim_decide.py` — claim post-dispatch decision → single Command
  - `_claim_present.py`— claim display: SSE publishing for the frontend timeline
  - `_llm.py`          — llm node (stream + cancel = discard partial turn)
  - `_llm_stream.py`   — llm streaming consumption (stall timeouts, non-stream fallback, cache retry)
  - `_llm_cancel.py`   — llm streaming-vs-cancel race (partial turn discard)
  - `_llm_chunk.py`    — llm chunk assembly + final-message validation
  - `_llm_errors.py`   — llm stream error taxonomy + consecutive-error tracking
  - `_exec.py`         — exec node (one disposable subprocess per execute_code call)
  - `_exec_output.py`  — code execution output envelope: format / truncate / overflow-to-file
  - `_exec_alerts.py`  — best-effort operator alert for boot-phase exec child crashes
  - `_build.py`        — build_graph: assemble 8-Node self-cycling topology
  - `_node_log.py`     — node enter/exit lifecycle log + publish timeline snapshot
  - `_system_prompt.py`— system prompt dynamic assembly (base + plugin contributions)

Public API is lazily re-exported via this __init__.py (PEP 562) — external
`from agent.graph import X` callers don't need to know the submodule layout,
and importing a light submodule (`_exec_protocol`, `_agent_traceback`) no
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
    from ._claim import claim_node as claim_node
    from ._exec import exec_node as exec_node
    from ._exec_output import EXEC_CANCEL_NOTE as EXEC_CANCEL_NOTE
    from ._llm import llm_node as llm_node

# Eager `from ._build import build_graph` used to run on every `agent.graph`
# import — pulling the full node set (build/claim/llm/exec and their trees)
# into every shell/files exec child. Resolve on first attribute access instead.
_LAZY_EXPORTS = {
    "build_graph": "._build",
    "claim_node": "._claim",
    "exec_node": "._exec",
    "EXEC_CANCEL_NOTE": "._exec_output",
    "llm_node": "._llm",
}

# Static checkers see the real signatures through the TYPE_CHECKING block
# above; at runtime `__getattr__` resolves the names lazily (PEP 562).
__all__ = [
    "EXEC_CANCEL_NOTE",
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
