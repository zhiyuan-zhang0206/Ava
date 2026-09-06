# Extension ownership: cluster content, machine capabilities, agent activation

> **Status: S1 landed (decision + spec revision); no code yet.** The issue sets
> the direction (three ownership tiers, five slices); this doc is the buildable
> elaboration: data model, capability vocabulary, resolution semantics, the
> fail-fast edge, executor semantics in the agent host, migration from
> today's per-machine state, and per-slice test locks.
>
> **S1 is done**: the ownership model is recorded durably in
> [`decisions/2026-08-21-extension-ownership-three-tiers.md`](../../decisions/2026-08-21-extension-ownership-three-tiers.md),
> and `conventions/plugin-spec-v2.md` S5 now says `machine` = capability set,
> `enabled_set` = cluster default + per-agent overlay, and splits
> `dependencies.hostCapabilities` into host requirements (matched for placement)
> versus resource access (context-gated at injection).
>
> **S2 is mostly built.** The `extensions` / `extension_blobs` tables and their
> constraints (the blob size cap, the repo-rows-carry-no-content iff),
> `shared/extension_registry.py`, and `shared/extension_materialize.py` all
> exist and are tested. `ava skill install` writes the cluster row + blob BEFORE
> touching local disk, and converge lands enabled `kind='skill'` rows onto each
> machine, refusing to overwrite a locally edited tree.
>
> The design's own S2 lock — *install on home A materializes on home B, two
> homes one PG* — is exercised by
> `tests/cli/test_extension_two_home_chain.py`.
>
> A prerequisite the slice walked into rather than introduced: the `extensions`
> migration is the first post-baseline one to CREATE a table, and a cluster's
> `ava_runner` read grant is a point-in-time loop issued once at install birth.
> So on a SPLIT deployment — the only posture where this slice's claim is
> non-trivial — the materializer hit `permission denied` and reported it as an
> unreachable registry. Fixed in `shared/cluster/provision.py` (standing
> `ALTER DEFAULT PRIVILEGES` + a re-affirm from `ava start` after a migration
> applies); the two-home fixture cannot see it, because it models two homes
> sharing one connection identity, not two credentials.
>
> Process boot materializes too (`agent/_process_boot.py:land_cluster_extensions`,
> and once per daemon in the hosted runner), which closes the window converge
> cannot reach: a machine that was down during an install, or a long-lived host
> that has been up since before it.
>
> The adoption sweep runs on every converge
> (`shared/extension_adopt.py:adopt_local_installs`): user-origin skills this
> machine installed before the registry existed become cluster rows, identical
> content on two machines merges in silence, and differing content is refused
> with both machines named.
>
> **Still open in S2**: the sync event. S3 onward is untouched.
>
> This doc remains where the buildable detail lives; the decision entry holds
> only what is durable (the tiers, the invariants, what was rejected).

## The problem, in one sentence

Plugins, MCP servers, and installed skills are owned by the **machine**
(`~/.ava/plugins/<name>/`, `~/.ava/plugins_config.json`, `~/.ava/mcp_enabled.json`,
per-machine `$AVA_HOME/skills/` installs tracked in `$AVA_HOME/installed.json`),
so two agents on two machines of one cluster can silently behave differently,
a plugin canary on one agent is not expressible, and extension state rides in
the same per-machine files whose rewrite already ate unrelated keys once
(the 2026-08 enroll `.env` incident). The cluster interior is supposed to be
fully controlled; per-machine installs are tolerated drift inside it — the same
ambiguity class that commit-pinned clusters (version) and the per-cluster data
plane (identity) eliminated, still open for extensions.

## The model

The machine stops being an *authored ownership* tier and becomes a *derived
constraint* tier:

| Tier | Owns | Form |
|---|---|---|
| **cluster** | Content + identity (which extensions exist: kind, version, hash, manifest, source) and the default enablement policy | Rows + content blobs in the cluster's Postgres; converge and process boot materialize trees onto each machine |
| **machine** | Only **capabilities** (os, arch, display, docker, login-session state, …). "Can this run here" is *computed* by matching manifest requirements against the capability set — never a hand-maintained enable file | Probed + declared set, registered beside `machine_units` |
| **agent** | Activation: the effective enabled set for this agent, as a delta over the cluster default | `agents_meta.extension_overlay`, carried by the spawn API like `config_overlay`, resolved by the executor at agent start |

Three invariants:

1. **One source of truth per fact.** Which extensions exist and their default
   enablement: cluster rows. What a machine can run: its capability set, always
   computed. What an agent runs: cluster default ± its own delta. Every
   per-machine file that survives is a **materialized cache** of cluster rows,
   never an authority.
2. **The fail-fast edge (non-negotiable):** "cluster-enabled but not runnable
   on machine M" is never a silent skip. The state is explicit and queryable
   ("P on M: not runnable, missing `display`"), and an agent whose own delta
   *requires* an extension is only placed on — and only boots on — a machine
   satisfying it.
3. **Activation is data; enforcement is the executor's job.** The resolved
   enabled set is derived from cluster rows + agent delta and recorded in the
   spawn/restart event trail. How it is enforced (import gating per process
   today, per-turn composition under a hosted runner) is an executor detail —
   see the PR #49 reconciliation below.

## Data model

Two new tables in the cluster's Postgres (schema sketch; exact DDL lands with
the slice migrations):

```sql
CREATE TABLE extensions (
    name            TEXT PRIMARY KEY,   -- match_key-folded identity (dash/underscore are one name)
    kind            TEXT NOT NULL CHECK (kind IN ('skill', 'plugin', 'mcp')),
    source          TEXT NOT NULL,      -- 'repo' | git URL | 'local:<machine>' (adopted / developed in place)
    source_ref      TEXT,               -- commit/tag as installed, when source is git
    version         TEXT,               -- ava-plugin.json manifest version, when present
    content_hash    TEXT NOT NULL,      -- tree hash of the landed content (install_registry.tree_hash)
    manifest        JSONB,              -- ava-plugin.json as landed; host requirements live here
    trust           TEXT NOT NULL DEFAULT 'unreviewed',  -- the skill-supply-chain tier, moved up with the row
    default_enabled BOOLEAN NOT NULL DEFAULT true,
    installed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE extension_blobs (
    content_hash TEXT PRIMARY KEY,      -- content-addressed; rows are immutable
    archive      BYTEA NOT NULL,        -- tar of the landed tree, IGNORED_NAMES excluded
    size_bytes   INTEGER NOT NULL       -- enforced cap at install time (source trees are small; big
                                        -- artifacts are host provisioning, not extension content)
);
```

Why content rides the DB rather than being re-fetched from the source URL on
each machine: the cluster's Postgres is the one shared data plane every unit
already reaches (an enrolled runner's connection facts include the DB URL);
re-fetching per machine re-buys network dependence, per-machine credentials for
private sources, and ref-moved drift — the exact non-fungibility this design
exists to kill. The blob is the *landed, scanned* tree: the supply-chain scan
and the manifest gate run once at install, and what was scanned is what every
machine materializes, byte-identical, verified by `content_hash`.

Repo-shipped content (`ava_builtins/plugins/`, `ava_builtins/skills/`)
does **not** move into these tables: it is already cluster-consistent via
commit-pinned code rollout, and its trust story is the checkout itself. The
registry owns what arrives by *install*, not by *release*.
`extensions.source = 'repo'` is therefore not a blob-backed row; repo sources
keep converging from the checkout exactly as `cli/commands/_converge_skills.py`
does today. What changes for them is only that enablement (today
`plugins_config.json` / registry `enabled` bits) becomes the cluster
`default_enabled` column.

`.agents/skills/` (the kernel-contributor project skills) is the exception:
since issue #146 it does **not** converge fleet-wide — it reaches agents
through the project-local mount
(`ava_builtins/plugins/ava_code/_walk.py:project_skill_roots`), so only agents
working inside the checkout see it.

The agent side is one column:

```sql
ALTER TABLE agents_meta ADD COLUMN extension_overlay JSONB;
-- {"enable": ["name", ...], "disable": ["name", ...]}; NULL = cluster default
```

— deliberately the same shape and plumbing as `config_overlay`: authoritative
in `agents_meta`, carried by `POST /api/agents` (and `agent_presets.config`),
replayed on restart/respawn/resurrect, inherited by a
fork, and recorded in the spawn/restart event trail for free.

## Capability vocabulary and matching

One closed vocabulary, defined once (a new `shared/capabilities.py`), one
matcher. Candidate initial set:

- `os:darwin` / `os:linux` / `os:windows`, `arch:arm64` / `arch:x86_64`
- `display` (a GUI session exists), `unix-socket` (AF_UNIX available)
- `docker`
- `gateway`, `agent-runner` — the existing `machine_role()` tokens, absorbed as
  capabilities rather than a parallel concept
- `login-session:<app>` — device state (the logged-in headed Chrome profile,
  a TCC-granted helper): modeled as a capability, never as an install

A machine's capability set = **probed** (os/arch/display/unix-socket/docker are
computed at `ava start`, the way `ava/_mcp_config.py` probes `display` today) ∪
**declared** (`login-session:*` and anything unprobeable, set by an operator
verb and persisted beside the machine's identity files). `register_self`
publishes the union to a `machine_capabilities` column/table beside
`machine_units`, so every capability question is answerable from the cluster DB
without reaching the machine.

Requirements come from the manifest. `conventions/plugin-spec-v2.md` S5 is
revised (slice S1) to split two axes that its `dependencies.hostCapabilities`
currently mixes:

- **host requirements** (`display: required`, `unixSocket: required`, os/arch
  constraints, `login-session:<app>`) — matched against the machine capability
  set by the one matcher. The MCP per-entry `requires` check
  (`ava/_mcp_config.py`) becomes a consumer of the same vocabulary and matcher.
- **resource access declarations** (`db: none|ro|rw`, `network`, `shell`) —
  these are context-model gates on what the extension may *touch*, not on
  where it can run; they stay in S5's context model unchanged.

An extension with no manifest declares no requirements and matches every
machine — the migration default that keeps every existing package behaving as
before.

## Enablement resolution and the fail-fast edge

The effective set for an agent A on machine M:

```
enabled(A, M) = (cluster default_enabled set  ∪ A.extension_overlay.enable
                                              ∖ A.extension_overlay.disable)
                filtered by runnable(·, M)
```

with these hard edges instead of silent filtering:

- **Overlay names must exist.** An overlay naming an unknown extension fails
  the spawn/restart request (same validation posture as
  `resolve_overlay_targets` for config keys).
- **An overlay-enabled extension is a placement constraint.** The spawn path
  (`POST /api/agents`) intersects the agent's required set with per-machine
  runnability *at placement time*: the agent lands on a capable machine, or the
  spawn is refused with the missing capabilities named. Boot re-checks and
  fails fast (capabilities may have changed since placement) — an agent never
  silently runs without something it explicitly asked for.
- **A default-enabled extension that is not runnable on M** is not a placement
  constraint (that would make one `display`-requiring plugin unschedulable the
  whole fleet onto headless machines); agents on M run without it, and the gap
  is a first-class queryable fact: `runnable(P, M)` is computed and exposed in
  `ava status`, `ava cluster status`, the inventory view, and the issue #41
  `ava plugins inspect` catalog as an explicit row — "P on M: not runnable,
  missing `display`" — never an absence you have to infer.
- **Enabled + runnable but content not materialized** is a fail-fast boot
  error naming the fix, not a skip (see materialization below — boot self-heals
  this case first, so the error only survives when the DB blob itself is
  unreachable).

This keeps invariant 2 honest in both directions: nothing loads less than
policy says without a queryable record, and nothing an agent explicitly
requires is ever quietly dropped.

## Executor semantics in the agent host

The agent host is the sole runtime. Per-agent activation must resolve at runtime
construction/turn boundaries, not by globally importing or unloading modules
for one agent. The host may import the union of locally needed plugins; each
turn's resolved set determines its hooks, state fields and SDK namespaces.
An extension overlay and its event trail remain the authority for enabling a
plugin for one agent. Import failures still have a host-wide fault boundary.

What this deliberately does *not* import from dsh: isolate realms / scoped
registration machinery. dsh needs them because many sessions share one process
*with no composition filter*; Ava's answer is the resolved-set filter at the
executor boundary, which is a dict lookup over attribution the `register_*`
calls already carry (`shared/plugin_context.py`), not a runtime realm.

## Converge, enroll, boot — who materializes what, and offline semantics

**Converge** loses authority, and materialization is invoked BESIDE the converge
steps rather than as one of them. `ava start` runs converge as step 1, brings
this cluster's Postgres up as step 2 and applies migrations as step 2.5, so a
`CONVERGE_STEPS` entry would read the registry before the database is up on a
single box, and before the `extensions` table exists on the rollout that creates
it (this doc originally described it as a step; #201 shipped it that way and the
correction is `cli/commands/_converge.py:materialize_cluster_extensions`, called
from `ava start` after the schema-current check and from the end of standalone
`ava converge`). The materializer itself pulls the enabled rows for this machine,
lands missing/stale trees from blobs (verified against `content_hash`), and
rewrites the local caches (`plugins_config.json`, `mcp_enabled.json`,
`installed.json` entries for registry-owned names) as derived state. The
existing tree-swap machinery (`_copy_tree` staging, user-edit hash guards,
`.preserved` markers) is reused as-is — only the *source* changes from "this
checkout / this machine's installs" to "the cluster registry".

**Process boot** runs the same `ensure_extensions_materialized()` for the
agent's own resolved set before `_load_extensions()`. This closes the offline
window structurally: a machine that was down during an install converges the
moment anything on it starts, and an agent never boots against a tree older
than the policy row it just read. Boot already requires the cluster DB (the
checkpointer), so this adds no new liveness dependency; if the DB is down,
agents were not booting anyway.

**`ava enroll`** changes not at all — which is the point. An enrolled runner's
cluster identity is the gateway URL + secret; its extension state is no longer
part of its identity, because it has none: first `ava start` after enroll
probes capabilities, registers them, and materializes exactly what the cluster
says. A new machine is fungible by construction — no "did we remember to
install the skills here" step, which is the drift-generator today.

**Staleness is bounded and visible**: between installs and the next converge on
a machine, its materialized set can lag the registry. The lag is detectable
(`content_hash` comparison, surfaced in the inventory view and `ava cluster
status`) and self-heals at the next process start on that machine. The S2 sync
event doubles as the issue #42 L2 broadcast carrier ("skill catalog changed"),
so live agents learn of new skills without waiting for a prompt rebuild.

## Migration from per-machine state

Adoption, not flag-day:

1. **Repo-origin rows** (converged repo skills, builtin plugins) need no
   content migration — only their enable bits move: a migration folds each
   machine's `plugins_config.json` / `mcp_enabled.json` / registry `enabled`
   bits into `default_enabled`. Where machines disagree today (the drift this
   issue exists to surface), the migration takes the **union of enabled** as
   the cluster default, and reports the disagreement it found — turning silent
   drift into a visible, reviewable diff at upgrade time.
2. **User-origin installs** (`origin="user"` rows in each machine's
   `installed.json`, external plugins in `~/.ava/plugins/`, `type="mcp"`
   packages): the first converge after upgrade uploads each unclaimed name
   (tree → blob, row with `source='local:<machine>'`). A name claimed by two
   machines with **identical** hashes merges silently; **differing** hashes
   refuse auto-adoption for that name and surface a converge warning naming
   both machines — resolution is an explicit operator pick, since either copy
   may carry local edits. Single box (the default posture) has no second
   machine, so adoption is trivially total.
3. **Local files demote to caches.** `plugins_config.json` and
   `mcp_enabled.json` stop being authorities and are rewritten by converge;
   `ava plugins enable/disable` / `ava mcp enable/disable` / the `PUT
   /api/inventory` write path retarget to the cluster row (`default_enabled`),
   turning today's cross-machine inventory UI from a per-machine toggle panel
   into a cluster-policy panel with a per-machine *runnability* column.
4. **Down path**: each slice's `.down.sql` drops its tables/column; content is
   already materialized on every machine, and the demoted local caches are
   valid pre-migration authorities again, so rollback loses nothing but the
   cluster-level view. Lossy steps (deleting local authority files) simply
   don't happen — the files stay, they just stop being read as truth.

## What legitimately stays per-machine

- **Secrets**: model API keys, per-machine exemptions — machine `.env`, as
  today. The extension system carries none.
- **Device state**: the logged-in Chrome profile, TCC grants — modeled as
  `login-session:*` / helper capabilities, not as installs.
- **Python environments**: an MCP package's venv is built per machine at
  materialization (`uv sync` against the landed tree), inside the spec-v2
  `pythonPackages` bounds; the *declaration* is cluster content, the *build* is
  machine-local by nature.

## Docs this revises when it lands

- `future/infra/decentralized-install-and-config.md` — its "install is a local
  operation" reframe and the 2026-06 "plugin-enable state went
  per-machine-local" reversal are **partially superseded**: install stays an
  agent-driven imperative act (no declarative engine, no marketplace — that
  half stands), but what it writes becomes a cluster row + blob, and enable
  state returns to the cluster as `default_enabled`. The overlay-retirement
  and single-source-`.env` config rulings are untouched.
- `future/infra/mcp-scope-and-bundling.md` — the open "formalize per-machine
  scope" item dissolves: a machine-singleton server (the shared headed
  browser) is expressed as host requirements (`display`,
  `login-session:chrome`) + cluster enablement, not a `scope: machine` field.
  The per-agent/machine *lifetime* axis for the daemon itself is unchanged.
- `conventions/plugin-spec-v2.md` S5 — `machine` = capability set (not install
  location); `enabled_set` = cluster default + per-agent overlay (not a
  per-machine file); `dependencies.hostCapabilities` splits into host
  requirements (matched) vs resource access (context-gated). The seven
  injection surfaces, the three payload runtimes, and the S3 lifecycle state
  machine are untouched.
- `decisions/2026-08-20-stop-fleet-distributing-kernel-contributor-skills.md`
  (issue #146) — ruled to stop converging `.agents/skills/` fleet-wide,
  sequenced after S2 lands. Landed alongside S2's materialization work: converge
  no longer enumerates `.agents/skills/` and cleans up the copies it used to
  land (see "Repo-shipped content" above). S2 as scoped above did not itself
  touch this: repo-shipped content still converges from the checkout; the stop
  is not a consequence of the registry rows S2 adds.

## Related work — must stay coherent with

- **Issue #41 (`ava plugins inspect`)**: the catalog's "what exists / what's
  enabled" half reads these cluster rows; its "what registered" half stays
  registration facts. The not-runnable row defined here is an inspect output.
- **Issue #42 (four-layer model)**: L3 develop-a-plugin installs into this
  registry and canaries on the author agent via `extension_overlay`; the S2
  sync event is L2's broadcast carrier; self-evolution's change detection
  extends to registry version changes.
- **PR #49 (runner-as-server)**: reconciliation table above.
- **#1212 (MCP gateway routing)**: the endgame where MCP becomes a cluster
  service reachable from any agent belongs there; slice S5 here only moves MCP
  *definitions + enablement* to the registry.

## Non-goals

- **Version canary** (side-by-side versions of one plugin per agent): needs
  versioned install dirs and interacts badly with swap-in silently picking up
  new code; the enabled-set canary covers "new plugin", and "new version for
  one agent" is explicitly out — record it in the S1 decision entry.
- **Per-machine sync tooling** as an alternative (rsync-style mirroring):
  manages the drift instead of eliminating it — rejected in the issue.
- **Runtime unification** (everything-is-a-plugin): rejected in
  plugin-spec-v2's borrow analysis; ownership/placement only.
- **A marketplace / publishing channel**: install stays git-URL + registry.

## Slices (each independently landable, in order)

| Slice | What lands | Test locks |
|---|---|---|
| **S1 — decision + spec** (landed) | `decisions/2026-08-21-extension-ownership-three-tiers.md` (three tiers; machine demoted to derived constraint; version-canary non-goal) + the plugin-spec-v2 S5 revision above, incl. the `hostCapabilities` split. No code. | — |
| **S2 — skills first** (tables + install-write + materialization landed, cross-machine chain locked; boot materialization, adoption sweep and sync event pending) | `extensions`/`extension_blobs` migrations; `ava skill install` writes row + blob; converge + boot materialization for `kind='skill'`; adoption sweep; the sync event. Skills are pure text, no runtime, no requirements — validates the whole chain at minimum risk. | install on home A materializes on home B (two homes, one PG); adoption conflict refused with both machines named; user-edit hash guard survives the source change |
| **S3 — per-agent activation** | `agents_meta.extension_overlay` migration; spawn API + preset + `ava.agents.spawn` field; resolution in `_load_extensions()` (or the turn boundary, per PR #49 ordering); event-trail recording. | overlay survives restart; overlay-disabled plugin is absent from the agent turn composition; unknown name refused at spawn |
| **S4 — plugins + capability matching** | plugin rows to the registry; `shared/capabilities.py` + probes + `machine_capabilities` registration; the matcher; placement constraint + boot re-check; `plugins_config.json` demoted to cache; not-runnable rows in status/inventory/inspect. | requirement-missing machine is refused as placement for a requiring agent; a default-enabled-but-not-runnable pair is queryable, not silent; cache rewrite is idempotent |
| **S5 — MCP** | `kind='mcp'` rows + `requires` → vocabulary unification; `mcp_enabled.json` demoted. Routing endgame stays #1212. | machine-singleton browser server expressed via requirements matches only the capable machine |
