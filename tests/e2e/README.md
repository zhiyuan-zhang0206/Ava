```markdown
# tests/e2e/ — Cross-process happy path

Run the real gateway / agent subprocess / Next.js dev server / Playwright Chromium,
mock the LLM path (scripted fixture instead of real Anthropic API).

Design doc: see `docs/superpowers/specs/2026-05-07-e2e-happy-path-design.md`
(local gitignored spec draft).

## Running

```bash
# Prerequisites: have Postgres/Redis server binaries locally (each worker runs
# independent temporary native clusters via tests/_containers.py, no Docker needed),
# npm install (frontend)
uv sync
.venv/bin/playwright install chromium

# Run all
.venv/bin/pytest tests/e2e/ -v

# See the real browser (development debugging)
HEADED=1 .venv/bin/pytest tests/e2e/ -v
```

On failure, full tracebacks are in `tmp/e2e-logs/{gateway,frontend}.log` and
`~/.ava/logs/agent-*.log`. CI failures also upload artifacts.

## Writing a new scenario

1. In `fakes/scenarios/`, add a module, define `SCRIPT: tuple[AIMessage, ...]`
   and `def build(model: str) -> ScriptedFakeChatModel`. The `build` signature must
   match the `agent/llm.py:_LLMFactory` Protocol (takes model name → returns
   BaseChatModel); the `isinstance(BaseChatModel)` at the end of `_resolve_override`
   catches bad factories immediately at build time.
2. The test function uses `@pytest.mark.scenario("tests.e2e.fakes.scenarios.<name>:build")`.
3. Each `AIMessage` in SCRIPT = one LLM turn:
   - turn calls tool: write `tool_calls=[{"id": ..., "name": "execute_code",
     "args": {"code": "<python source>"}}]`
   - turn only replies: write `content="..."`, `tool_calls=[]`
   - each must carry `usage_metadata` (claim node asserts it is non-empty)
4. The test itself uses the `e2e_env` fixture to get `gateway_url / frontend_url / agent_url
   / page / agent_id`. **Default: `page.goto(e2e_env.agent_url)`** — it already
   carries the `?agent_id={spawned_agent}` deep-link query, letting the useAgents mount effect
   read the URL param and directly init activeId, bypassing the "sidebar agents fetch completes before auto-select"
   race. To test "auto-select fallback when no agent is specified" use bare
   `frontend_url`.

`ScriptedFakeChatModel` emits the entire message in a single chunk — does not simulate character-level
streaming. If future tests need scenarios like "streaming cancellation mid-way", extend the fake to support
multi-chunk.

## Key constraints

- **CI runs e2e serially (`-n 1`, dedicated `ava-ci-e2e` box)** — only one full stack runs per machine at a time,
  for resource determinism (a stack gets exclusive CPU, timing-sensitive lifecycle waits are not preempted
  by sibling stacks on the same machine). Not a port constraint: gateway/frontend ports are dynamically
  allocated by the kernel (`_ports.py`); Postgres/Redis use tests/_containers.py to start independent
  temporary native clusters per worker (random ports). So concurrent e2e runs are actually isolated from
  each other, they just consume resources — running them concurrently locally is also fine.
- **dev and e2e can run simultaneously** — ports/databases/channels are all separate
  (see `_ports.py` + `conftest.py` env override section).
- **gateway is function-scoped** — restarts per test with fresh `AVA_LLM_OVERRIDE`
  env; frontend / browser are session-scoped (slow to start, not related to env injection).
- **lifecycle daemons publish the start boundary** — the bare gateway fixture opens
  a fresh generation before gateway launch; after its health checks, the process
  restarter or hosted agent-host marks that generation serving. The fixture clears
  it after each test, because the file-backed marker outlives database truncation.
- **AVA_* env forwarding**: the gateway launches an agent as a detached, native process
  with an explicitly built child env dict (`ops.agent_launch.agent_spawn_env_dict`), so the
  test's `AVA_*` overrides reach it. Sessions (daemons, agent shells) need the same
  explicitness — the env is handed over out-of-band, never argv — and get it from the
  built env dict / 0600 envfile (argv is world-readable, issue #974).

## Current scenarios

| File | scenario | What it validates |
|---|---|---|
| `test_self_terminate.py` | `lifecycle_terminate` | `ava.self.terminate` → status='terminated' + inbound source='self' |
| `test_self_restart.py` | `lifecycle_restart` | `ava.self.restart` → restarter respawn → new PID + status back to idling |
| `test_self_resurrect.py` | `lifecycle_resurrect` | after `terminate`, `POST /resurrect` → fresh process + 'resurrect' inbound |
| `test_fork_identity.py` | `fork_identity` | `POST /api/agents` fork_from=source+prompt → forked agent (new id) first claim batch contains [fork marker, prompt], reply `FORK_OK` proves context contains both |
| `test_message_flow.py` | `message_flow` | **Panoramic Case 1 (#1018)** — one user message → reasoning + code tool call + real exec + reply; REST timeline fan-out (reasoning/code/output/chat), reply rendered in browser via SSE, zero unrecognized-marker alarms + zero `[timeline] unrecognized` console warnings |
| `test_compact_flow.py` | `compact_flow` | **Panoramic Case 2 (#1018)** — UI-triggered force compact (POST /api/agents/{id}/compact) → Compaction LLM (script turn) → clean wipe → `inbound_compact_request` envelope renders, NO unrecognized-marker alarm (#1017 regression), agent replies post-compact |
| `test_error_recovery.py` | `error_recovery` | **Panoramic Case 3 (#1018)** — LLM raises FatalProviderError (no retry) → SSE `error` event → `[error]` marker in browser (NOT the unrecognized alarm), aborted turn commits no agent_chat, next message recovers normally |

**Differences between fork scenario and lifecycle**: fork creates a **new agent_id** (not reused).
`build()` distinguishes source / forked process by whether there is a `kind='fork'` inbound for
`ava.self.AGENT_ID` (fork inbound is committed before launch, only present for forked agent). The
forked branch's fake does not follow a fixed script, but inspects the first round of real `messages`
received — containing the identity marker (`"forked from agent:"`) + prompt before replying
`FORK_OK`, turning "whether the forked agent truly sees both" into an assertable reply. The source
agent must run one round to build a checkpoint first, so the fork has something to copy from
(an idle empty agent has no checkpoint → `ForkSourceEmpty`).

**Two-segment SCRIPT pattern for lifecycle scenarios**: for `lifecycle_restart` / `lifecycle_resurrect`,
the fake `build()` checks `inbound_messages` for rows with `kind='restart_completed'` /
`kind='resurrect'` to distinguish first / post-restart-or-resurrect process; the first
process follows the lifecycle trigger SCRIPT, and the later segment follows an idle SCRIPT
(defensive, actually not consumed). Checking `inbound_messages` rather than `messages`:
restarter / `resurrect_agent` INSERT of these rows is the only definitive marker that
"a new process has started", and the `kind` field precisely corresponds.

## Scope (not done in this phase)

- Real character-level LLM streaming
- Multi-turn cross-agent interactions
- Record-and-replay (fake always follows SCRIPT)
- Visual regression
- Performance baseline
- pytest-xdist parallelization
```
