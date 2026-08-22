# Logical CWD Is Not Process CWD

## Context

The `ava_code` plugin persists a per-agent working directory in LangGraph
state. It also changed the Python process cwd when the value was set or restored
from a checkpoint. That made plugin state a process-global side effect. An agent
whose logical cwd pointed at an older Ava checkout then launched
`python -m agent.exec_child`; Python put that cwd first on `sys.path`, the old
top-level `agent` package shadowed the installed checkout, and the new
`agent.exec_child` module could not be found.

The exec subprocess is a fault-isolation boundary: the parent can terminate
stuck native code without killing the agent. It is not a security sandbox.

## Decision

`ava.cwd` is logical state owned by the AvaCode plugin. `get()` and `set()` read
and write that state only. AvaCode's file, shell, and understand wrappers resolve
relative paths from it explicitly. Neither checkpoint restore nor `set()` calls
`os.chdir`; bare Python filesystem calls, imports, and user subprocesses keep the
stable cwd of their Python process.

The core executor does not interpret AvaCode state and adds no cwd field to its
IPC protocol. Each disposable child starts from the agent process's stable cwd
with the same venv. The parent uses list-form `Popen(argv)` for the direct OS
process-creation path (`exec` / `posix_spawn` on POSIX, `CreateProcess` on
Windows), with no shell or command-string dependency:

```text
python -I -X utf8 -m agent.exec_child
```

Isolated mode keeps the inherited cwd, user site, and `PYTHON*` environment out
of trusted bootstrap import resolution. Explicit UTF-8 mode replaces the
encoding guarantee that `-I` would otherwise discard with `PYTHONUTF8` and
`PYTHONIOENCODING`, including on Windows.

## Alternatives rejected

- **Pass logical cwd to `Popen` or `chdir` inside the child.** Rejected because
  it recreates process-global cwd semantics and lets plugin state influence
  trusted bootstrap or bare Python behavior.
- **Absolute launcher.** Rejected because isolated module execution solves
  bootstrap identity without adding a packaging entry point or installation-
  path discovery contract.
- **A new cwd IPC field.** Rejected because it would duplicate state already
  present in the request snapshot and make the core executor interpret an
  AvaCode plugin concept.
- **`multiprocessing` spawn, fork, or forkserver.** `spawn` still starts and
  reimports an interpreter while adding pickle and Windows `__main__` bootstrap
  coupling. Fork and forkserver are POSIX-only; fork is unsafe around the
  multithreaded async parent, while forkserver adds a long-lived coordinator and
  serialization. Changing the launcher API alone does not repair `sys.path`
  precedence, and none improves import isolation or cancellation over list-form
  `Popen`.
- **Threads.** Rejected because stuck native code is not reliably killable and
  cwd remains shared process state.
- **Long-lived worker or socket pool.** Rejected because it adds queue
  backpressure, cross-run state leakage, and worker recovery while weakening the
  fresh-process boundary. A warm one-shot pool is reconsidered only if production
  telemetry shows cold-start latency violates an explicit SLO.

## Consequences

- One agent's logical cwd cannot alter framework imports or another execution
  flow's relative-path base.
- AvaCode SDK calls observe a same-turn `set()` immediately through plugin
  state. Bare `open`, `Path.cwd`, imports, and user-created subprocesses do not;
  callers that need logical resolution use the SDK wrappers or explicit paths.
- A stale checkpoint cwd is validated after restore and repaired to the agent
  workspace without changing process state.
- Existing process-group cancellation, timeout escalation, output streaming,
  result envelopes, and fresh-process isolation remain unchanged.
