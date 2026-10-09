```markdown
# tests/e2e/ — Cross-process happy path

Run the real gateway / agent subprocess / Next.js dev server / Playwright Chromium,
mock the LLM path (scripted fixture instead of real Anthropic API).

Design doc: see `docs/superpowers/specs/2026-05-07-e2e-happy-path-design.md`
(local gitignored spec draft).

What is (and is not) covered, feature by feature, lives in [FEATURES.md](FEATURES.md).


SDK creation and budget-handoff scenarios explicitly request the
`authenticated_gateway` fixture. It gives their test HTTP clients and exec SDK
children a fresh private bearer; the throwaway gateway verifies it through
production authentication middleware. Runner/ops retain the direct-process
harness's open posture because no root launcher or machine-token ledger exists.
Other scenarios retain their existing gateway posture. Fixture credentials are
function scoped and restored at teardown; no product admission bypass is added.


## Running

```bash
# Prerequisites: have Postgres/Redis server binaries locally (each worker runs
# independent temporary native clusters via tests/_containers.py, no Docker needed),
# npm install (frontend)
uv sync
.venv/bin/playwright install chromium

# Run one scenario file (the whole directory is CI's e2e job, never a local run)
.venv/bin/pytest tests/e2e/flow/test_message_flow.py -v

# See the real browser (development debugging)
HEADED=1 .venv/bin/pytest tests/e2e/flow/test_message_flow.py -v
```

On failure, full tracebacks are in `tmp/e2e-logs/{gateway,frontend}.log` and
`~/.ava/logs/agent-*.log`. CI failures also upload artifacts.

## Writing a new scenario

1. In `fakes/scenarios/`, add a module, define `SCRIPT: tuple[AIMessage, ...]`
   and `def build(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel`.
   The `build` signature must match the `base/lm/factory.py:_LLMFactory` Protocol
   (model name and explicit agent id → BaseChatModel). Host model factories must
   use this id for records and scenario selection; the host does not bind the
   process-local `ava` SDK. None denotes a caller outside an agent. The
   `isinstance(BaseChatModel)` at the end of `_resolve_override`
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
  by sibling stacks on the same machine). The gateway owns one bound socket from module import through
  frontend build and per-test restarts. POSIX `pass_fds` transfers it directly to the existing
  `python -m uvicorn --fd` entry; normal gateway lifespan is unchanged. Each previous fixture process
  must exit before replacement; readiness requires HTTP plus the new PID/birth's native listener.
  Fixed-port listener diagnostics retain native identities and unknown visibility on failure.
  Next's frontend port still uses a released kernel allocation and has a separate reservation gap;
  dynamic allocation alone is not a zero-collision guarantee. Postgres/Redis use tests/_containers.py
  for independent native clusters per worker. This gateway FD path targets the existing POSIX E2E CI.
- **dev and e2e can run simultaneously** — ports/databases/channels are all separate
  (see `_ports.py` + `conftest.py` env override section).
- **gateway is function-scoped** — restarts per test with fresh `AVA_LLM_OVERRIDE`
  env; frontend / browser are session-scoped (slow to start, not related to env injection).
- **lifecycle daemons publish the start boundary** — the bare gateway fixture opens
  a fresh generation before gateway launch; after its health checks, the process
  restarter or hosted agent-host marks that generation serving. The fixture clears
  it after each test, because the file-backed marker outlives database truncation.
- **AVA_* env forwarding**: sessions (daemons, agent shells) get an explicitly built env —
  handed over out-of-band, never argv — from the built env dict / 0600 envfile (argv is
  world-readable, issue #974).

## Current scenarios

| File | scenario | What it validates |
|---|---|---|
| `lifecycle/test_self_terminate.py` | `lifecycle_terminate` | `ava.self.terminate` → status='terminated' + inbound source='self' |
| `lifecycle/test_self_restart.py` | `lifecycle_restart` | `ava.self.restart` → restarter respawn → new PID + status back to idling |
| `lifecycle/test_self_resurrect.py` | `lifecycle_resurrect` | after `terminate`, `POST /resurrect` → fresh process + 'resurrect' inbound |
| `lifecycle/test_fork_identity.py` | `fork_identity` | `POST /api/agents` fork_from=source+prompt → forked agent (new id) first claim batch contains [fork marker, prompt], reply `FORK_OK` proves context contains both |
| `flow/test_message_flow.py` | `message_flow` | **Panoramic Case 1 (#1018)** — one user message → reasoning + code tool call + real exec + reply; REST timeline fan-out (reasoning/code/output/chat), reply rendered in browser via SSE, zero unrecognized-marker alarms + zero `[timeline] unrecognized` console warnings |
| `flow/test_compact_flow.py` | `compact_flow` | **Panoramic Case 2 (#1018)** — UI-triggered force compact (POST /api/agents/{id}/compact) → Compaction LLM (script turn) → clean wipe → `inbound_compact_request` envelope renders, NO unrecognized-marker alarm (#1017 regression), agent replies post-compact |
| `flow/test_error_recovery.py` | `error_recovery` | **Panoramic Case 3 (#1018)** — LLM raises FatalProviderError (no retry) → SSE `error` event → `[error]` marker in browser (NOT the unrecognized alarm), aborted turn commits no agent_chat, next message recovers normally |
| `state/test_shell_history_state.py` | `shell_history` | **task #4585** — browser back/forward between `/?agent_id=N` and `/shell/N/S` restores each container's scroll position (per history entry) with no blank / invalid-params frame on either return, and the shell poll + manual refresh stay alive |
| `plugins/test_ava_code_effects.py` | `ava_code:*` (`build_cwd_notes`, `build_context_files`, `build_cwd_tools`, `build_cwd_restart`, `build_system_prompt`, `build_after_compact`) | **ava_code effects, asserted on the messages the model actually received** (a recording fake logs each call's input), the files the exec wrote, and the checkpoint: cwd-change note + project-skills note reach the next model input and are consumed; AGENTS.md / CLAUDE.md injected on read once (path + content-hash dedup, direct-read exempt, oversize head+tail with archive); relative SDK paths follow the logical cwd; cwd survives restart and a vanished cwd falls back to the workspace; coding conventions are in the system prompt; compaction re-surfaces skills + context files |
| `lifecycle/test_lifecycle_effects.py` | `lifecycle_effects:*` | **Agent lifecycle effects, asserted on what each agent's model was handed, inbound rows and disk**: `ava.agents.send_message` reaches the peer's model with the sender in the inbound; `ava.agents.spawn` starts a child that runs its prompt; external terminate stores a message that greets the revival; forced terminate and `/api/cancel` kill a running exec (its post-sleep marker never appears) and the agent stays usable; `ava.self.compact` replaces history with the summary |
| `flow/test_sdk_effects.py` | `sdk_effects:*` | **execute_code and SDK effects, asserted on the tool output / notes the model is handed back and on disk**: a raising or hard-crashing exec is reported and the agent carries on; `ava.files.edit` missing/ambiguous/replace_all contract; reading injection-like content yields a SECURITY note (source and triggers, never the body); a `/command` expands into the model input and is listed by `ava.agents.commands()`; an uploaded file is announced to the agent and readable at the announced path; an exec timeout kills the child and its own subprocess |
| `flow/test_schedules_api.py` | `schedules:build` | **Schedule management through real gateway and CLI processes**: REST CRUD and version snapshots; start/stop/restart persist a sync request for the absent manager; runs and transcript reads; draft launches a writer whose model sees the request; `ava schedules` verbs reach the same gateway rows. Session execution is covered separately by SC2/SC3. |

The test files sit in subdirectories by domain, which also keeps `tests/e2e` under its direct-entry budget: `flow/` (full-turn panoramic cases), `lifecycle/` (restart, resurrect, terminate, fork, impersonation), `state/` (browser scroll and navigation state), `visual/` (layout, accessibility and snapshot tests with their `_layout_assertions.py`, `_visual_snapshot.py` helpers and the `__snapshots__/` goldens). The shared helpers (`_db.py`, `_env.py`, `_ports.py`, `_proc.py`, `_settings.py`, `_truncate.py`) and `conftest.py` stay at the top level, so the package-scoped fixtures cover every subdirectory.

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

## Visual regression

- `visual/test_visual_regression.py` — three full-page snapshots (home, fleet,
  mobile) against `tests/e2e/visual/__snapshots__/test_visual_regression/`, compared
  with the browser-native pixel diff (0.1% ratio, channel delta 16).
- `visual/test_preview_visual_gate.py` — the five-surface post-deploy matrix (same
  shared engine as the deployment gate: `scripts/post_deploy_visual/matrix.py`)
  against the committed goldens under
  `tests/e2e/visual/__snapshots__/preview-gate/`. Blocking on every PR. Goldens are
  minted and refreshed only on the ubuntu CI runner via the
  visual-baselines workflow (`workflow_dispatch` on the PR head) — generation
  and comparison share one rendering environment; a structurally broken run
  can never become the golden.

The browser-free `lifecycle/test_budget_handoff.py` exercises the usage observer
as a separate process: a new fork joins an already observed spawn lineage,
actual telemetry crosses a threshold, and the reminder reaches the owner's model
input. Scripted goal and orchestration decisions preserve partial results,
leave peers alive, and honor a saved pause after a late checkpoint and restart.
Every JSON handoff writer publishes through the shared atomic-file helper, so
cross-process readers see complete snapshots during prepare, dispatch and pause.
`fakes/test_budget_handoff.py` holds a real child writer before publication and
checks that readers retain the previous snapshot until replacement.
This validates runtime composition, not unprompted model compliance with skills.

## Scope (not done in this phase)

- Real character-level LLM streaming
- Unscripted model judgment in multi-turn cross-agent interactions
- Record-and-replay (fake always follows SCRIPT)
- Performance baseline
- pytest-xdist parallelization
```
