"""llm node: invokes the LLM to stream Python code generation + cancel handling.

Package door — no imports, no re-exports; callers use `agent.graph.llm.node`
(re-exported as `agent.graph.llm_node` via the parent package's lazy
`__getattr__`). Modules:

  - `node.py`    — llm node (stream + cancel = discard partial turn)
  - `_stream.py` — llm streaming consumption (stall timeouts, non-stream fallback, cache retry)
  - `_cancel.py` — llm streaming-vs-cancel race (partial turn discard)
  - `_chunk.py`  — llm chunk assembly + final-message validation

The llm stream error taxonomy (`agent/graph/llm_errors.py`) lives in the parent
package: code outside this package classifies those errors too.
"""
