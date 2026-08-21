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
  - `_build.py`        — build_graph: assemble 8-Node self-cycling topology
  - `_node_log.py`     — node enter/exit lifecycle log + publish timeline snapshot
  - `_system_prompt.py`— system prompt dynamic assembly (base + plugin contributions)

Public API is re-exported via this __init__.py — external
`from agent.graph import X` callers don't need to know the submodule layout.
"""

from ._build import build_graph
from ._claim import claim_node
from ._exec import exec_node
from ._exec_output import EXEC_CANCEL_NOTE
from ._llm import llm_node

__all__ = [
    "EXEC_CANCEL_NOTE",
    "build_graph",
    "claim_node",
    "exec_node",
    "llm_node",
]
