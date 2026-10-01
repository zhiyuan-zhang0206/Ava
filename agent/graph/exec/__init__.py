"""exec node: one disposable child process per `execute_code` call.

Package door — no imports, no re-exports; callers use `agent.graph.exec.node`
(re-exported as `agent.graph.exec_node` via the parent package's lazy
`__getattr__`). The door stays import-free on purpose: the exec child imports
`protocol.py` before user code runs and must not drag the node set in with it
(agent/tests/test_lazy_child_imports.py). Modules:

  - `node.py`        — exec node: run one pending call, dispatch the result sum type
  - `output.py`      — output envelope fed back to the LLM: format / truncate / overflow-to-file
  - `protocol.py`    — request/result envelopes shared with the child entry (`agent/exec_child.py`)
  - `_subprocess.py` — parent side: spawn / poll / signal / collect one child
  - `_owned_run.py`  — the same run under an admitted durable resource set
  - `_process.py`    — owned lifetime of one child process tree (reap, domain close, reader join)
  - `_stream.py`     — streaming stdout/stderr capture + incremental publish
  - `_result.py`     — exec outcome sum type + the child-crash placeholder
  - `_crop.py`       — soft size previews with reference-protected archives
  - `_notes.py`      — in-memory system-note injection after the tool results
  - `_alerts.py`     — best-effort operator alert for boot-phase child crashes

The child entry itself stays at `agent/exec_child.py`: `-m agent.exec_child` is
a runtime process entry another checkout's parent may spawn, so its module path
is a cross-version contract.
"""
