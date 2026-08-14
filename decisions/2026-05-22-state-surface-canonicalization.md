# Every state surface belongs to one of four quadrants

> **Decision date 2026-05-22**, triggered by the WSL `DanglingPlugin` incident the
> same day (it superseded the narrower `plugins-config-cross-machine-drift.md`
> written hours earlier). **Reconciled 2026-06-01** with
> [`decentralized-install-and-config.md`](../future/infra/decentralized-install-and-config.md)
> (the later, 2026-05-30 direction).
>
> What survived and still governs: the **four-quadrant frame** (mutability ×
> scope ⇒ storage) and the "`~/.ava/` must be wipe-and-replay safe" rule. What
> the reconciliation overturned: plugin enable/config is **per-machine local**,
> not cluster-wide DB state (so migration step 4 "config → DB" is **reversed** —
> see the Reconciliation section), and the overlay legitimately holds
> **registry-tracked external installs**, so "no third place" sharpens to "no
> *ungoverned* place." The 2026-06-01 live audit found both hosts' overlays clean,
> closing the trigger incident.
>
> Since then, config moved further still: the override layer was retired
> 2026-06-08 and config is single-source in each unit's `.env` — the quadrant
> answer for Q4 config is no longer "a DB row"
> ([`2026-07-19-config-ownership-decomposition.md`](2026-07-19-config-ownership-decomposition.md)).

## Reconciliation with the decentralized-install model (2026-06-01)

The later [`decentralized-install-and-config.md`](../future/infra/decentralized-install-and-config.md)
reframed install as a **local** operation, which moves where some of the state
below should live. Mapping that onto the four-quadrant frame (which itself still
holds):

- **The frame is right; one classification was wrong.** This draft put *plugin
  enable/config* in Q4 (cluster-wide → DB). The decentralized model reclassifies
  it as **per-machine** — a plugin is enabled on the hosts where it is installed,
  not cluster-wide — so it is per-host **local config**, not a DB row. Genuinely
  cluster-wide runtime state (agent state, inbound messages, schedules) is still
  Q4 → DB.
- **"No third place" → "no *ungoverned* place."** The overlay legitimately holds
  per-host facts that *do* affect behavior: machine identity, local config, and
  **registry-tracked external installs** (`~/.ava/installed.json`,
  `shared/install_registry.py`). The bug was never "a file under `~/.ava/`
  affects behavior"; it was **leaked repo content / untracked dirs** (the manual
  `dup` plugin, the stale `~/.ava/skills/` repo copies). Repo-shipped
  skills/plugins ride `git pull` and load from the repo; the overlay is reserved
  for what the install registry tracks. The skill-scanner overlay reservation
  (surface only registry-listed overlay skills) already enforces this.
- **Net rule, reconciled:** `~/.ava/` = per-host **identity + local config +
  registry-tracked external installs + regenerable cache**; the **repo** holds
  code (incl. repo-shipped skills/plugins/MCP); the **DB** holds genuinely
  cluster-wide runtime state. Memory stays the deliberate git-synced exception.
  The directory must still be `rm -rf`-and-replay safe — only identity is
  irrecoverable (a one-line `machine_name` / `machine_serve_*` / `.env`), the rest
  self-heals on `ava start` (or re-install from the recorded source).

Per-step status of the original migration plan is annotated inline below.

## Trigger incident

WSL agent-runner could not start agents. `shared/plugins_config.py:load()` raised
`DanglingPlugin` for an external plugin named `dup`. The chain:

1. Someone manually created `~/.ava/plugins/dup/` on the gateway host —
   a stub with one line, `__description__ = 'dup plugin'`.
2. Gateway's `load()` auto-merge added `dup: {enabled: true}` to the central DB
   row.
3. WSL agent-runner had no such directory on disk. Its `_discover_plugins()` did
   not contain `dup`. The fail-fast `dangling = cfg.plugins - known` check fired
   and the gateway refused to start.

Patched by removing `dup` from both the DB row and the gateway filesystem. The
patch did not address the structural reason the failure was possible — the same
shape exists for several other state surfaces today, some of which fail
silently rather than loudly.

## The four-quadrant inventory

Every piece of state in Ava sits on two axes:

- **Mutability**: changed only via git commit (edit-time), or via API/SDK/UI at
  runtime?
- **Scope**: must all hosts in the cluster agree, or is per-host variance
  acceptable?

|              | Per-host acceptable      | Cluster-wide required |
|--------------|--------------------------|------------------------|
| **Edit-time** | Q1 — machine identity   | Q2 — code              |
| **Runtime**   | Q3 — local ephemeral    | Q4 — cluster state     |

Each quadrant admits a clean storage choice:

- **Q1**: a tiny per-host file or env var. `machine_serve_gateway` /
  `machine_serve_agent_runner`, `machine_name`, `.env` (secrets).
- **Q2**: the git tree, `<repo>/`. Synced via `ava update` (gateway) and
  `ava update-self` (agent-runner).
- **Q3**: per-host filesystem, regenerable. `logs/`, `*.pid`, `*.sock`,
  `milvus-data/`, etc.
- **Q4**: central Postgres. Plugin enable flags, runtime config overrides,
  agent state, inbound messages, schedules, etc.

## What is broken: `~/.ava/` holds Q2 and Q4 things

Walking the actual `~/.ava/` tree on both hosts:

| Path | True category | Today's storage | Failure mode |
|------|---------------|-----------------|--------------|
| `~/.ava/plugins/<external>/` | Q2 (code) | Q3 (per-host fs, no sync) | **This week's incident.** DB references a plugin name; some host lacks the file → `DanglingPlugin` on startup. |
| `~/.ava/configs/<plugin>/config.json` | Q4 (cluster state) | Q3 (per-host fs) | UI edits gateway's copy; agent-runner agents read stale local copy. Silent drift. |
| `~/.ava/skills/<user>/` | Q2 (code) | Q3 | Gateway has 7 user skills; agent-runner has none. `ava.skills.<name>` resolves differently per host. |
| `~/.ava/mcp.json` | Q2 or Q4 | Q3 | Only gateway has it. Agents on agent-runner call into `ava.mcps.*` with an empty registry. |
| `~/.ava/monitors/<id>.py` | Cache of DB `monitors.code` | Dual-written | Monitor cannot migrate between hosts. "Source of truth" ambiguous. |
| `~/.ava/memory/` | Q4, document-shaped | Per-host git branch + merger agent | Works, via a fifth sync mechanism unique to this surface. |
| `~/.ava/swebench-repos/` | Bench-specific | Gateway only | Acceptable today (bench is gateway-only); needs an explicit "gateway-only by convention" tag. |

## Diagnosis: five sync mechanisms, no rule

Ava currently uses five different mechanisms to keep state coherent across
hosts:

1. Central DB (Postgres rows, read/write via `psycopg`).
2. Git tree, bulk-synced via `ava update*`.
3. Git tree, per-host with a merger agent (memory only).
4. **Nothing** — per-host fs treated as authoritative ("just write it locally
   and hope").
5. Local-only by design (logs, pids, sockets — not a sync problem at all).

Where a given surface lives, today, is the product of history rather than
principle. The plugin incident is loud because plugin loading is fail-fast; the
disk-image, skill, and MCP cases are quieter but the same shape — anything
sitting in mechanism (4) that ought to be in (1) or (2) drifts the moment hosts
diverge.

## Proposed rule

> **Superseded by the reconciled rule at the top** (the decentralized-install
> model). Below is the 2026-05-22 two-place version; it was too strong on two
> points — plugin enable/config is per-host local (not DB), and the overlay
> legitimately holds registry-tracked external installs (a governed third
> location). Kept as the original framing.

> `~/.ava/` is per-host **cache + identity**, not a source of truth. The
> directory should be deletable with `rm -rf` and self-heal on `ava start`.
> Only (Q1) machine identity and (Q3) regenerable cache live there.
>
> Any fact that the agent or framework reads to decide behavior lives in
> exactly one of two places:
>
> - **Code layer** (`<repo>/`): plugin files, skill files, MCP server
>   definitions, default-config schemas — anything that ships with a version,
>   synced by git.
> - **DB layer**: plugin enable flags, runtime overrides, scheduled inbounds,
>   agent state — anything mutable at runtime that must be cluster-coherent.
>
> No third place.

The discriminator collapses to two questions (still useful, with the
reconciled answer for the per-host case):

1. Can this fact change without a git commit? *No* → code layer (repo).
2. Must **all hosts** agree on it? *Yes* → DB layer. *No, it's per-host* →
   local overlay (identity / local config / registry-tracked install), not DB.

Memory (`~/.ava/memory/`) remains a deliberate exception — document
collaboration genuinely benefits from git's revision/branch/merge primitives,
and async sync is acceptable for notes. It must be marked as an exception,
not treated as the seed of a third general category.

## Migration plan (ordered by cost)

Status annotations (2026-06-01) reflect the reconciliation above.

1. **MCP config** → **partly landed.** Resolved not by moving the file but by the
   Layer I MCP loader (`ava/_mcp_config.py:load_mcp_config`) merging machine
   `mcp.json` + plugin-bundled `.mcp.json`, plus the `ava mcp` CLI. Per-host MCP
   availability is intended (the decentralized model — `browser-use` lives where
   it's installed), not drift. Remaining shape in
   [`mcp-scope-and-bundling.md`](../future/infra/mcp-scope-and-bundling.md).
2. **External plugins** (`~/.ava/plugins/`) → **reframed, not deleted.** The
   overlay is the *intended* home for **registry-tracked external installs**
   (decentralized-install pieces A/B landed: `installed.json` +
   `ava plugins install` + Claude Code plugin materialization). Only *leaked repo
   copies / untracked dirs* (the `dup` incident) are the bug, and the audit shows
   the overlay is clean today. Repo-shipped plugins still live in `<repo>/ava_builtins/plugins/`.
3. **User skills** (`~/.ava/skills/`) → **done for the leak; reframed for installs.**
   Leaked repo skill copies removed; the skill-scanner overlay reservation surfaces
   only registry-listed overlay skills. Repo-shipped skills stay in the repo;
   externally-installed skills are registry-tracked in the overlay (intended).
4. **Plugin runtime config** (`~/.ava/configs/<n>/config.json`) → **REVERSED.**
   This draft proposed moving config *into* Postgres; the decentralized model goes
   the opposite way — plugin enable/config is **per-machine local**, and the
   long-term target is to **remove** the DB config tables (`plugins_config_overrides`
   removed — migration 0035; `runtime_config_overrides` retired 2026-06-08 — config is
   now single-source in `.env`;
   the emptied table is dropped in a later expand-contract migration). So `~/.ava/configs/`
   on disk is aligned with the target, not a defect; the "one switch for many
   machines" convenience becomes a frontend online-aggregation concern. See
   [`decentralized-install-and-config.md`](../future/infra/decentralized-install-and-config.md)
   "Config & enablement = local".
5. **Monitor files** → **still valid (doc-only).** Daemon writes them from
   `monitors.code` on launch; never read back. Disk file is cache, DB is truth.
6. **Bench repos** → **still valid.** Keep gateway-only, document the
   convention explicitly.

## Trade-offs

What we gain:

- Zero cross-host drift for facts. Drift is only possible for caches, which by
  definition do not affect behavior.
- `~/.ava/` becomes wipe-and-replay safe — disaster recovery on a single host
  reduces to `rm -rf ~/.ava && ava start`.
- New contributors no longer need to learn "which paths are git, which are DB,
  which are orphan."
- Fail-fast semantics on plugin loading recover their original meaning
  (`DanglingPlugin` = real bug, not partial sync).

What we give up:

- Local prototyping of a plugin or skill requires a git commit. There is no
  "drop a file in `~/.ava/plugins/`" path. Given the project is single-author
  and worktree-based, the cost is one commit per experiment.
- Runtime config edits remain non-hot-reload (agent restart still required).
  This was already the case — moving storage from disk to DB does not change
  reload semantics.

## Open questions

- **Migration of existing disk images.** Each host today has its own
  `~/.ava/configs/<plugin>/config.json`. Which host's copy seeds the DB?
  Recommended: gateway wins, agent-runner diff logged to console for review.
- **Plugin runtime config edit UX.** Today the UI is implicitly per-host
  (because the file is per-host). Centralizing means edits propagate at next
  agent restart, cluster-wide. (Reversed — see item 4 above: `plugins_config_overrides`
  was since *removed* and plugin config is per-host local, so there is no centralized
  parity to match.)
- **MCP servers that genuinely vary per host.** If a future MCP server binds
  to a gateway-only resource, the registry needs a `primary_only: true` flag
  analogous to the bench repos convention.
- **`~/.ava/.env`.** Secrets are arguably Q4 but kept out of DB for
  blast-radius reasons. Status quo (manual sync, gateway-authoritative) is
  acceptable; out of scope for this proposal.

## References

- `shared/plugins_config.py:67-93` — `_discover_plugins()` two-directory scan
  (repo + registry-tracked overlay; the overlay scan is intended, not collapsed).
- `shared/plugins_config.py:175-215` — `load()` `DanglingPlugin` check.
- `shared/plugin_config_registry.py` — `bind_from_disk()` **stays** disk-backed
  (reconciled: config is per-machine local; the earlier `bind_from_db()` plan is
  reversed).
- `shared/install_registry.py` + `~/.ava/installed.json` — the governed overlay
  registry the reconciled rule rests on.
- `ava/skills.py` — repo + registry-tracked overlay skill sources (the overlay
  reservation that fixed the leaked-skill drift).
- `ava/mcps.py:60-74` — `mcp.json` path resolution, to migrate.
- `ava/monitor.py:44-47` — `_monitors_dir()` cache, to mark non-authoritative.
- `okf/skills.ava.okf.md` — the skills dual-source loading description, to update.
