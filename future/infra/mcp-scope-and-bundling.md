# MCP scope & bundling

> **Status: the source axis is fully landed; the scope axis is not.** The only
> remaining work is **formalizing per-machine scope** as a first-class category.
>
> Landed — all three **sources**, merged by one loader
> (`ava/_mcp_config.py:load_mcp_config`, read by both the in-process tool surface
> and the connection daemon): built-in `ava_builtins/mcps/<name>/.mcp.json`,
> plugin-bundled `.mcp.json`, the installed registry package (`type="mcp"`,
> `ava mcp install` → `$AVA_HOME/mcps/<name>/`), and the machine `mcp.json` on top
> (machine overrides a plugin default). Also landed: **per-agent** scope, the
> default — one daemon per agent (`agent/mcp_daemon.py`, socket keyed by `agent_id`).
>
> Not landed: the per-entry `{source, scope}` **schema**. There is no `scope` field
> in an MCP entry, so a server cannot *declare* that it is a machine-level singleton;
> the one real machine-scope consumer (the shared headed browser) is hard-wired as a
> bespoke service rather than expressed through the model below — see "Open".
> [`extension-ownership.md`](extension-ownership.md) (issue #39) proposes
> dissolving the `scope: machine` field into host-capability requirements
> (`display`, `login-session:*`) matched against the machine's capability set —
> if that lands, this open item closes without a scope field.
>
> This doc captures the model that must hold for the rest of Layer I; the "plugin =
> bundle skills + MCP + hooks" gap is in
> [`decentralized-install-and-config.md`](decentralized-install-and-config.md).

## The reframe: only two scopes

An MCP server is a **live process holding a resource** — unlike a skill (read-only
prompt text). That forces a scope/lifetime axis skills never had. After cutting
the two non-categories below, only two scopes remain:

- **per-agent** — one instance per agent. Tied to the agent's working context,
  needs isolation, warm connection is valuable. This is **today's model**: each
  agent process spawns one MCP daemon (`mcp_daemon.<agent_id>.sock`) that holds
  every server's connection across the agent's ephemeral per-turn exec
  subprocesses.
- **per-machine** — one instance per machine, shared by all agents. Owns an
  exclusive or shared resource. **Not expressible today**: the per-agent daemon
  starts a *copy* per agent, which breaks any singleton resource (port conflict,
  device contention) and duplicates any browser-wide buffer N times. Where it was
  genuinely needed, it was built by hand — see "Open".

## Why there is a daemon at all (context)

The per-agent scope above is shaped by Ava's sandbox model: each `execute_code`
turn is a **fresh subprocess that dies when the turn ends**. If MCP connections
lived in that subprocess, every turn would cold-start every server (≈250ms each
+ the server's own warm-up — a heavy server startup is far worse). So each agent process
spawns **one long-lived daemon subprocess** that holds the connections; the
per-turn exec subprocesses talk to it over a Unix socket
(`agent/mcp_daemon.py`, `ava/_mcps_daemon.py`) instead of starting servers
themselves. The daemon is connection-persistence infrastructure, not optional.

The **per-machine** scope needs the same idea one level up: a daemon **not** keyed
by `agent_id`, so a single instance survives across agents — which the current
per-agent daemon cannot express.

## Why "ephemeral MCP" is not a category

A stateless, cheap capability (web search, a fetch, time) is **an SDK function /
HTTP call, not an MCP** — `ava.web.search` is just a request. There is no server
to keep warm, so there is nothing to scope. If you would wrap it as an MCP, you
wouldn't. Drop it.

## Why "per-identity" is not a (framework) category

Assume **every agent acts as one identity — "me."** Per-identity inside the
framework explodes complexity: `mcp.json` is one machine-level file with static
env, so it cannot express "agent A uses token X, agent B uses token Y," and
per-agent credential injection is a large separate concern.

When a genuine separate identity is needed, it stays at the **application/agent
layer**, two ways, neither a framework scope:

- the agent **self-provisions a temporary MCP** (writes config / starts a server)
  and the framework **reload/connects** to it — e.g. an agent that registered its
  own Slack account spins up its own Slack MCP; or
- a purpose-built identity MCP is designed **one-machine-one-identity**
  (= per-machine).

The cases that *look* per-identity are already covered by existing primitives:

- **Gmail / Calendar** — agents already message each other; calendar-style
  scheduling is `Scheduler` + `Monitor`.
- **Filesystem** — per-agent separation is per-agent worktrees / folders, not a
  separate FS-MCP instance.
- **Memory / vector store** — already a centralized design.

## Survey (reference)

| MCP | resource | scope |
|---|---|---|
| headless / isolated browser (fresh) | own user-data-dir context | per-agent |
| browser attach to a real Chrome | one logged-in Chrome + fixed CDP port | per-machine |
| Docker / container runtime | the machine's one docker daemon | per-machine |
| shared DB proxy | connection pool to a fixed external DB | per-machine |
| Slack / Gmail (if built) | one identity's token | per-machine (one-machine-one-identity) |
| web search / fetch / time | stateless HTTP | not an MCP (SDK function) |

## Consequence for Layer I (plugin-bundled MCP)

An MCP declaration carries `{source, scope}` — not just a config dict:

- **source** — where the config comes from. **Landed:** the machine `mcp.json`
  and plugin-bundled `.mcp.json` (declared like CC's, at the plugin root) are
  merged by `ava/_mcp_config.py:load_mcp_config` — machine overrides a plugin's
  same-named default — which both `ava/mcps.py:_load_config` (in-process) and
  `ava/_mcps_daemon.py:_load_config` (the daemon) now delegate to. **Also
  landed:** `ava plugins install` can now *deliver* a plugin-bundled `.mcp.json`
  into `~/.ava/plugins/<name>/` (`cli/commands/_claude_code_plugin.py`, alongside any
  bundled agents) — feeding the existing plugin source, no new loader path.
  **Landed (2026-07-19):** a standalone installed registry package (`type="mcp"`)
  as a third source. `ava mcp install <git-url|local-dir>` lands a self-contained
  package (own `.mcp.json` + `pyproject.toml`) under `$AVA_HOME/mcps/<name>/`,
  runs `uv sync` to build its isolated `.venv`, and registers it in
  `shared/install_registry.py`. `load_mcp_config` scans it as the installed layer
  (`_installed_mcp_paths`, registry-gated), between plugin and machine. The
  package's command is a relative `.venv/bin/python -m <module>` spawned with
  cwd = the package dir (`installed_mcp_dir`), so its deps stay out of core — no
  `uv run` (a resident wrapper). Discord was the first server moved out this way
  (as a standalone package), removing `discord-py` from core deps — removed entirely on 2026-08-12 (user ruling: no longer maintained). `ava mcp
  install` mirrors `ava plugins install` (shared `cli/commands/_pkg_source.py`).
- **scope** — `agent` (default; current per-agent daemon) | `machine` (a new
  machine-level daemon: socket not keyed by `agent_id`, shared, ref-counted by
  connected agents). No `identity` field — single-identity assumption.

## Reload / connect

An agent that just provisioned an MCP (updated config, or started a temp server)
should **connect without a full restart**. Lazy connect already re-reads config
on first use of a server name; a `reload` / `connect` affordance makes the
self-provision path explicit and is the mechanism the per-identity escape hatch
above relies on.

## Open (not now)

- **PR2c shipped groundwork (2026-08-22).** The runner-backed `agent_skill_view`
  result now carries the runner host's sorted enabled MCP server names alongside
  its per-agent command catalog. Nothing consumes that field yet. Phase 2 still
  owns per-agent MCP scoping, gateway exposure, and frontend wiring; an MCP
  consumer must not reconstruct the catalog from the gateway's filesystem.

- Machine-level daemon: lifetime / ownership (who starts it, ref-count teardown
  when the last agent disconnects), socket location, and the in-process →
  machine-daemon connect path.
  - **Update (2026-05-31):** the first machine-scope consumer — a shared headed
    browser — landed WITHOUT an Ava-built MCP daemon. The shared singleton was the
    **browser process**, run as a runner-owned `ServiceSpec` (`browser`,
    opt-in `AVA_BROWSER_ENABLED`); agents reached it through an ordinary per-agent
    `chrome-devtools-mcp` (stdio) attaching via `--browserUrl`. So "machine scope"
    reduced to the existing long-running-session + converge primitives, not a new
    daemon. See `services/browser/daemon.py` + `cli/commands/_repo.py` (`browser`) +
    `cli/commands/_converge.py:_ensure_browser` (writes/removes the attach plugin
    `$AVA_HOME/plugins/ava_chrome/.mcp.json` gated on `AVA_BROWSER_ENABLED`, so the
    MCP is contributed only where the browser runs).
  - **Update (later): sharing the browser process was not enough — the *upstream*
    had to be shared too, and that forced a hand-built machine-level daemon.**
    `chrome-devtools-mcp`'s collectors subscribe to the whole browser's targets, so
    N per-agent upstreams each buffered every tab's network/console traffic — an
    N-fold duplication that dominated agent-runner memory. `services/browser/
    mcp_daemon.py` is now a per-machine shared upstream multiplexed over a Unix
    socket to each agent's bridge (`services/browser/mcp_wrapper.py`), with two
    invariants making one upstream safe for many clients: a single lock serializing
    every upstream interaction (one browser op at a time, machine-wide), and
    per-connection page affinity (each connection tracks its own current page and
    the daemon re-selects it before any page-scoped call, so one client's `click`
    cannot land on another's tab).

    **So the second consumer arrived — and it was the same consumer, one level
    deeper.** This is the concrete case for formalizing `scope: machine`: what
    exists is a bespoke browser-specific daemon, and the two invariants above
    (serialization + per-connection resource affinity) are exactly what a generic
    machine-scope daemon would have to provide. Generalizing it is the remaining
    work — lifetime / ownership, ref-counted teardown, socket location, and the
    in-process → machine-daemon connect path, with the browser daemon as the
    reference implementation rather than a special case.

    **Update (2026-08-03):** the generic machine-scope daemon now exists — the
    shared MCP daemon (`ava/_mcps_daemon.py`, ops roster session "mcp-daemon",
    per-connection session isolation) plus a `"shared"` server spec. Chrome's
    per-agent bridge process is gone (the wrapper module itself still exists at
    `services/browser/mcp_wrapper.py` and remains the command chrome's `.mcp.json`
    declares): the daemon dials
    the browser-mcp service's line protocol in-process (`ava/_mcp_browser.py`),
    keeping per-connection page affinity because each connection owns its socket.
    Stateless servers (x) declare `"shared": true` for one daemon-wide
    stdio child serialized per server.
  - **Update (2026-05-31, later):** chrome's config moved off the converge-written
    file into a first-class **built-in `mcps/` folder** — `<repo>/ava_builtins/mcps/<name>/.mcp.json`,
    auto-scanned by `load_mcp_config` as the lowest-precedence source (symmetric
    with built-in `skills/` + `plugins/`). converge no longer writes it; it just
    sheds the legacy file. A declarative **`requires`** precondition on a server
    entry (e.g. `{"display": true}`, `ava/_mcp_config.py:assert_requirements`) is
    checked before connect and yields a clear capability error on incapable hosts
    instead of an opaque "connection refused". The runtime **`register_mcp_source`**
    leg (plugins contributing MCP configs at runtime, symmetric with Layer H
    `register_skill_source`) is designed but deferred — the snag is the per-agent
    MCP daemon subprocess doesn't load plugins, so an in-process provider doesn't
    reach it; candidate cross-process fixes (agent materializes resolved config /
    daemon goes config-agnostic) are recorded in the design history.
- The per-entry `{source, scope}` schema on `mcp.json` entries + plugin
  `.mcp.json`. (The merge precedence — machine config overrides a plugin
  default — is settled and landed; what remains is tagging each entry with its
  source/scope so the daemon can route by scope.)
- Whether a plugin-bundled MCP change requires a daemon restart or rides
  `reload`.
