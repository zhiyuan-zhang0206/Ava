# Self-cycling LangGraph with long-await nodes

## Context

An Ava agent is one OS process, one LangGraph thread, one unit of work. Its lifetime *is* a loop: wait for an inbound message, call the LLM, run the emitted code, wait again. There is no natural end — a persistent agent runs until it terminates itself or is killed.

LangGraph 1.x recommends a different shape for this. The reactive pattern is `interrupt() + Command(resume=...)`: a node calls `interrupt(value)` to pause the graph and checkpoint; an external caller wakes it with a fresh `graph.ainvoke(Command(resume=...))`. Every wake is a new invocation. The official `GRAPH_RECURSION_LIMIT` guidance explicitly marks a self-cycling graph as an anti-pattern and `recursion_limit=∞` as something to avoid.

That guidance is written for a specific deployment shape — multi-tenant cloud, where one worker serves tens of thousands of threads and *must* release the worker when a thread is idle so the autoscaler can scale down. Ava is the opposite shape: a single-tenant, long-running local kernel where one thread owns one process. Holding a worker while idle is not a leak — it is the design. There is no other tenant waiting for that worker, and the wake signal arrives over `LISTEN/NOTIFY`, not across an HTTP boundary.

## Decision

Each agent process runs one self-cycling LangGraph invocation:

```python
graph.ainvoke({}, config={"thread_id": str(agent_id)}, recursion_limit=∞)
```

It runs forever, looping `claim → llm → exec → claim`, until the agent terminates or is killed. The `claim` node is a **long-await node**: it blocks on `LISTEN/NOTIFY`, holding the worker until an inbound message arrives. `recursion_limit=∞` is set deliberately, declaring to the framework that this unbounded loop is intentional business logic — not a runaway.

The recursion guard is a runaway safety rail: it catches loops that *shouldn't* happen but did. Ava's loop is a real business loop, the same way an HTTP server's `while True:` accept loop is its own semantics rather than a bug. Turning the guard off is the correct statement of intent.

State stays minimal — `messages` and `halted` only. Routing rides on `Command(goto=...)` at runtime, not in state. Each node completion is a checkpoint; on process restart the graph recovers from the last checkpoint.

## Alternatives rejected

**LangGraph mainline `interrupt() + Command(resume=...)`.** The framework-recommended path: `claim` calls `interrupt({"reason": "wait_inbound"})`, the graph pauses and checkpoints at that boundary, and an external caller resumes with `ainvoke(Command(resume=msg))` on each new message. Rejected because every premise behind it is false for Ava:

- *"Pause must release the worker."* Only true under multi-tenant pressure. One-thread-one-process has no contention to relieve.
- *"Resume is triggered across an HTTP boundary (a frontend HITL popup)."* Ava's wake is `LISTEN/NOTIFY`, directly reactive with no HTTP boundary to hang resume off of.
- *"Pods are evicted anytime, so the checkpoint boundary is the recovery anchor."* Ava processes are long-running; restarts are rare, and crash recovery via the inbound table's claimed state is sufficient. The interrupt/resume ceremony buys nothing.

Adopting it would mean every inbound becomes a separate external `ainvoke` — an external driver loop reintroduced on top of the graph, which is exactly the dual-track (loop-plus-graph) coupling the rearchitecture removed. It is needless complexity for the single-tenant long-running scenario.

**`langchain.agents.create_agent` (the ReAct middleware harness).** The framework's blessed agent runtime, with `before_model` / `after_model` / `wrap_model_call` / `wrap_tool_call` hooks. Rejected as structurally incompatible at the execution-model level:

| create_agent assumes | Ava is |
|---|---|
| tools = JSON tool_call schema | tools = raw Python `execute_code` |
| loop = model → tool → model | loop = claim → llm → exec → claim |
| halt = model stops calling tools | halt = a lifecycle exception raised from exec |

The middleware hook points are hard-wired to model/tool boundaries and cannot attach to Ava's claim / end-of-turn boundaries. What transfers is the *pattern* (composable units), not the code.

**Building the plugin layer on `AgentMiddleware`.** Its hook names (`before_model`, etc.) are fixed and ReAct-loop-specific; they don't line up with Ava's `claim / pre-llm / post-exec / end-turn` boundaries. Rejected in favor of graph-level nodes and conditional edges — arbitrary state→state functions whose hook points are defined together with the graph topology, so the graph topology *is* the hook topology.

## Consequences

- **Deviation from mainline is a standing maintenance cost.** Long-await nodes are an unsupported pattern, so every LangGraph minor upgrade must re-verify that a self-cycling graph with a blocking node still runs. Mitigated by a CI smoke test that exercises the self-cycling prototype.
- **A reverse migration to multi-tenant cloud would be expensive.** The long-await model would have to be torn out and replaced with interrupt/resume. This is an accepted, explicitly out-of-scope trade — Ava is single-tenant by design.
- **Borrowing non-conflicting 1.x APIs is still fine.** `Runtime[Context]` for dependency injection and `BaseCallbackHandler` for token streaming sit beside the chosen path and are used; only the interrupt/resume control model is rejected.
- **The loop is the business, so an unbounded recursion limit is honest, not reckless** — but it shifts the burden of stopping onto explicit lifecycle signals (terminate / restart / idle) rather than onto the framework's guard.
