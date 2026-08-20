# Extensions are owned by the cluster; the machine is a derived constraint

## Context

Plugins, MCP servers and installed skills are owned by the **machine**:
`~/.ava/plugins/<name>/`, `~/.ava/plugins_config.json`, `~/.ava/mcp_enabled.json`,
and per-machine `$AVA_HOME/skills/` installs tracked in `$AVA_HOME/installed.json`.
Three consequences follow, and all three are ambiguity rather than policy:

- two agents on two machines of one cluster can silently behave differently,
  because nothing makes the two machines agree;
- a plugin canary on one agent is not expressible at all — enablement has no
  per-agent tier, so trying a new plugin means changing what every agent on that
  machine gets;
- extension state rides in the same per-machine files whose whole-file rewrite
  already ate unrelated keys once (the 2026-08 enroll `.env` incident).

The cluster interior is otherwise fully controlled. Commit-pinned clusters closed
the *version* ambiguity ("which code is this machine running"); the per-cluster
data plane closed the *identity* ambiguity ("whose Postgres is this"). Extensions
are the same class, still open: per-machine installs are tolerated drift inside a
boundary that is supposed to have none.

This entry records the direction ruled in issue #39 and elaborated in
[`future/infra/extension-ownership.md`](../future/infra/extension-ownership.md).
It is slice S1 of that plan — the decision and the spec revision, no code. The
data model, capability vocabulary, resolution semantics and per-slice test locks
live in that doc; what is durable, and therefore here, is the ownership model and
what was rejected to get it.

## Decision

**Three ownership tiers, with the machine demoted from an authored tier to a
derived one.**

| Tier | Owns | Form |
|---|---|---|
| **cluster** | Content and identity — which extensions exist (kind, version, hash, manifest, source) — and the default enablement policy | Rows + content blobs in the cluster's Postgres; converge and process boot materialize trees onto each machine |
| **machine** | Only **capabilities** (os, arch, display, docker, login-session state, …). "Can this run here" is *computed* by matching manifest requirements against the capability set | A probed + declared set, never a hand-maintained enable file |
| **agent** | Activation — the effective enabled set, as a delta over the cluster default | `agents_meta.extension_overlay`, carried by the spawn API like `config_overlay` |

Three invariants come with it:

1. **One source of truth per fact.** Which extensions exist and their default
   enablement: cluster rows. What a machine can run: its capability set, always
   computed. What an agent runs: cluster default ± its own delta. Every
   per-machine file that survives becomes a **materialized cache** of cluster
   rows, never an authority — `plugins_config.json` and `mcp_enabled.json` are
   demoted rather than deleted.
2. **The fail-fast edge is non-negotiable.** "Cluster-enabled but not runnable on
   machine M" is never a silent skip. The state is explicit and queryable ("P on
   M: not runnable, missing `display`"), and an agent whose own delta *requires*
   an extension is only placed on — and only boots on — a machine satisfying it.
3. **Activation is data; enforcement is the executor's job.** The resolved
   enabled set is derived from cluster rows + agent delta and recorded in the
   spawn/restart event trail. How it is enforced — import gating per process
   today, per-turn composition under the hosted runner — is an executor detail
   that can change without changing the ownership model.

**Version canary is an explicit non-goal.** Side-by-side versions of one plugin,
differing per agent, is out of scope: it needs versioned install directories, and
it interacts badly with a hibernation swap-in silently picking up new code. The
enabled-set canary covers "try a *new* plugin on one agent", which is the case
that motivated per-agent activation; "a *different version* for one agent" is
not bought by this design and should not be assumed from it.

## Alternatives rejected

- **Status quo — per-machine ownership.** This is the 2026-06 ruling that moved
  plugin-enable state per-machine-local, and it is what is being reversed. It was
  right about install being an imperative, agent-driven act and wrong about where
  the resulting *state* belongs: it made every machine independently authoritative
  about a fact the cluster is supposed to own, which is precisely the drift.
- **Per-machine sync tooling (rsync-style mirroring).** Manages the drift instead
  of eliminating it: a mirror is still N authorities plus a process for keeping
  them equal, and "keeping them equal" is the thing that fails silently. Rejected
  in issue #39.
- **Re-fetching content from the source URL on each machine**, rather than
  storing blobs in the cluster DB. Re-buys network dependence at materialize
  time, per-machine credentials for private sources, and ref-moved drift — the
  exact non-fungibility this design removes. The DB is the one shared data plane
  every unit already reaches, and the blob is the *landed, scanned* tree, so the
  supply-chain scan runs once and what was scanned is what every machine gets,
  byte-identical and hash-verified.
- **A `scope: machine` field on MCP servers** to express machine-singleton
  servers (the shared headed browser). A second ownership vocabulary for one
  case; the same fact is already expressible as host requirements (`display`,
  `login-session:chrome`) matched against the capability set. This dissolves the
  open "formalize per-machine scope" item rather than answering it.
- **Runtime unification (everything-is-a-plugin).** Rejected in plugin-spec-v2's
  borrow analysis and not revisited here — this decision is about ownership and
  placement only.
- **A marketplace / publishing channel.** Install stays git-URL + registry; a
  store is infrastructure a single-user deployment does not need.

## Consequences

- **Three docs are partially superseded**, each in a specific way rather than
  wholesale. `future/infra/decentralized-install-and-config.md`: install stays an
  imperative agent-driven act (that half stands), but what it writes becomes a
  cluster row + blob, and enable state returns to the cluster as
  `default_enabled`; its overlay-retirement and single-source-`.env` rulings are
  untouched. `future/infra/mcp-scope-and-bundling.md`: the open per-machine-scope
  item dissolves as above, while the per-agent/machine *lifetime* axis for the
  daemon is unchanged. `conventions/plugin-spec-v2.md` S5: revised by this slice
  (below).
- **A new failure mode has to be surfaced, not swallowed.** Making the machine a
  constraint tier means "enabled but not runnable here" becomes a real, reachable
  state. Invariant 2 is the whole reason it is acceptable: if that state is ever
  a silent skip, this design is strictly worse than per-machine files, because
  the drift becomes invisible instead of merely unmanaged.
- **The cluster DB carries content.** Blob size is capped at install time; source
  trees are small, and large artifacts are host provisioning, not extension
  content. This is a deliberate widening of what the data plane holds.
- **#146 is unblocked by S2, not by this entry.** The ruling to stop converging
  `.agents/skills/` fleet-wide is sequenced to ride the S2 registry landing;
  repo-shipped content keeps converging from the checkout until then. Nothing in
  S1 changes that interim state.
- **Slicing is load-bearing.** S2 does skills first — pure text, no runtime, no
  host requirements — so the whole chain (row + blob + materialize + adoption) is
  validated at minimum risk before plugins and capability matching arrive in S4.

## The plugin-spec-v2 S5 revision this slice carries

`conventions/plugin-spec-v2.md`'s context model is
`Context = {ava_home, db_role, cluster_scope, machine, enabled_set}`. Two of
those five dimensions meant something that this decision changes, and one
manifest field splits:

- **`machine`** is a **capability set**, not an install location. Nothing resolves
  "is this plugin here"; the question is "can this machine run it", answered by
  matching manifest requirements against probed capabilities.
- **`enabled_set`** is **cluster default + per-agent overlay**, not a per-machine
  file. `plugins_config.json` / `mcp_enabled.json` become caches of the cluster
  rows.
- **`dependencies.hostCapabilities`** splits along an axis it currently conflates.
  Some keys are **host requirements** — facts about the machine, matched for
  placement (`display`, `unixSocket`). Others are **resource access** — what the
  package may reach, gated by the context at injection time (`db`, `network`,
  `shell`). The first decides *where* a package can run; the second decides *what
  it can touch once running*. Keeping them in one bag makes placement matching
  read like a permission system and permissions read like hardware detection.

The seven injection surfaces, the three payload runtimes, and the S3 lifecycle
state machine are untouched by this decision.
