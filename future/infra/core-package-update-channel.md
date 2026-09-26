# Core package update channel — decoupling core skill/plugin updates from the cluster update

Status: design for implementation (authored 2026-09-11; user rulings 2026-09-11 and 2026-09-13). Implementation: P0 landed (registry schema v2, `ava packages status`, derived host version — PR #2355) and P1 landed (content-channel executor — `ava packages refresh`/`rollback`/`policy` + OS job + rollout skip + host filter — PR #2368); remaining: P2 core-plugin materialization, then P3 alignment with [extension-ownership](extension-ownership.md).

Ruling record: [`decisions/2026-09-13-core-package-update-channel.md`](../../decisions/2026-09-13-core-package-update-channel.md) — it revises the delivery half of `decisions/2026-08-19-four-layer-modification-model.md` ruling 2.

Related: [extension-ownership](extension-ownership.md), [decentralized-install-and-config](decentralized-install-and-config.md), [release-dir-atomic-code-swap](release-dir-atomic-code-swap.md), [`conventions/plugin-spec-v2.md`](../../conventions/plugin-spec-v2.md), [`conventions/host-versioning.md`](../../conventions/host-versioning.md).

## 0. Summary

1. Two of the three asks are partially satisfied by accident of existing design; the third does not exist at all.
2. **Core content is repo-coupled today — that stays.** The change is not an API boundary; it is a *delivery* boundary: core skills/plugins get their own update channel (their own cadence), while the repo remains their single authoring/review home.
3. Skills are already hot (single load dir, copy-on-write, no restart). The missing piece is *who refreshes them, when*: today only a human runs `ava skill update`, or a full `ava cluster update` rollout runs it once. The 2026-08-27 incident (a repo skill edit invisible in runtime for two days) is exactly this gap.
4. Plugins are not decoupled at all: built-in plugins are checkout/wheel code, so any plugin edit anywhere requires the whole L4 train (PR → CI → merge → cluster update → restart). Decoupling them means materializing the core plugin set where the runtime can load it independently — and, as of the 2026-09-11 loader unification (#2985 / PRs #2201, #2206: one contract, package-relative imports resolved for external plugins, fail-soft on every load path), the standard external root is a viable destination. What remains for P2 is the collision rule (a materialized copy of a builtin name is currently a fail-closed duplicate), module-identity verification, and the isolation gates (§3.9) — the development pause itself lifted 2026-09-11 10:06.
5. **Recommended mechanism: a per-machine "content channel" refresh** — fetch the repo remote's content paths (objects only, never the prod checkout tree), apply per package with the existing stage-swap / hash-guard machinery, driven by a per-package policy (mode + interval) and a converge-registered OS job. `git submodule` is *not* recommended as the runtime mechanism (it fights the "prod checkout = installed commit, reset + clean" invariant, and its pinning value is reproducible with a lock file). Moving core content into its own distribution repo is the right *evolution* when a publication pressure appears; the channel is designed so that move is a source swap, not a rewrite.
6. **User rulings (2026-09-11) adopted**: one uniform default — refresh every **24h** for every channel-backed package, **manual refresh** always available, the core channel tracks **`main`**; and a **version/compatibility model** (§5.5) is now part of the design: packages declare a semver `version` plus `engines.ava` (min **and** max host bounds), enforced at install, at refresh landing, and at load; violations keep old content or skip a package loudly — they never brick a process. Skills gain optional manifest support for this.
7. POC scope: **P1 skills fast lane end-to-end** (policy + channel + refresh + version gates + observability), P2 core-plugin materialization + activation-at-boundary (pause lifted 2026-09-11; isolation gates apply), P3 alignment with the extension-ownership work (issue #39, S4).

## 1. The three requirements, against the system as built

Source of requirements: the user's 2026-09-11 request (task #2915).

### R1 — "core plugin / core skill may stay coupled to this repo (a core skill may reference SDK names directly)"

**Today: already true.** Core skills live in `<repo>/ava_builtins/skills/`, plugin-carried skills in `<repo>/ava_builtins/plugins/<p>/skills/`, core plugins in `<repo>/ava_builtins/plugins/<p>/`. They ship, get reviewed, and break — under the same PR/CI as the kernel. The plugin manifest (`ava-plugin.json`) already carries `version` / `engines.ava` / `dependencies` with a validator and install-time host-version check (`shared/plugin_manifest.py`, spec: `conventions/plugin-spec-v2.md`).

**Gap: none structural.** What is missing is the *stated policy*, because this design's whole point is that core content leaves the L4 train for updates — and without a stated contract, the next natural step ("version your content API") would silently re-couple the cadence. The contract to state:
- core content MAY reference internal SDK names, paths, and behaviors of the checkout it targets; no stable third-party API is promised;
- executable core content (plugins) carries a compatibility gate (`engines.ava`) checked at *every* landing, including channel refreshes;
- text content (skills) is fail-soft by design — a skill that references a not-yet-present SDK name fails visibly in the transcript when used, and never breaks a service.

### R2 — "but they update independently, at a different frequency than the main cluster (e.g. daily)"

**Today: half true for skills, false for plugins.**

- Skills: the runtime reads ONE load directory (`$AVA_HOME/skills/`); repo built-ins are *synced copies*, so a copy refresh is hot (next skill scan / `ava.help()` — "active on the next skill scan; no restart needed"). But the refresh moments are only two:
  1. an explicit `ava skill update` (R5 ruling: converge lands a missing copy and *never* updates one — updates are the explicit verb, task #1013);
  2. the rollout legs (`_update_local._refresh_builtin_skills` / `_update_agent_runner._refresh_builtin_skills`) which run `ava skill update` once per `ava cluster update`; conflicts are non-fatal.
  Nothing is periodic; nothing is automatic. A repo skill edit reaches a running machine only when a human types the verb or a rollout happens.
- Plugins: built-in plugins load from the checkout/wheel (`paths.repo_plugins_dir()`; provider plugins load from the same discovery in every model-building process). Their content moves only when the *code* moves (uv sync + service restarts). `ava plugins upgrade <name>` works only for packages with a recorded git *source*; a repo-origin package is explicitly refused with "it updates via `ava cluster update`".

**Gaps:** ① no independent delivery channel for core content; ② no materialized runtime root for core plugins (skills have one; plugins do not); ③ no automatic refresh at all.

### R3 — "install-time option: auto-update on/off + check interval (e.g. provider plugin 6h, skill 2h); high-frequency skill edits must not disturb the cluster"

**Today: absent.** The install registry row (`$AVA_HOME/installed.json`, schema v1) has no update fields; there is no periodic executor for package refresh; there is no status surface. To build: a policy plane (mode + interval, per package), an executor (who checks, when), and observability.

**No disruption is already half-guaranteed by physics, not by policy:** skills are hot by construction. Plugins activate at process start — so an update can always *land* without touching a running service, at the cost of activation being deferred to the next process boundary. The design must state this explicitly, and never auto-restart services for content.

## 2. Machinery that already exists (reuse — do not rebuild)

| Piece | What exists | Where |
|---|---|---|
| Single skills load dir | `$AVA_HOME/skills/`; converge syncs repo built-ins + plugin-carried skills into it; user installs land directly | `cli/commands/_converge_skills.py`, `okf/skills/load-directory-sync.ava.okf.md` |
| Install registry (per machine) | origin (`repo`/`plugin`/`user`), trust tier, `content_hash` / `installed_hash` (R5 edit guards), `enabled`, schema `version` field as a migration anchor | `shared/install_registry.py` |
| Explicit update verbs | `ava skill update [name...] [--force]` (repo-native), `ava skill upgrade <name>` (git-sourced), `ava plugins upgrade <name> [--force]`, `ava mcp upgrade` — all with the R5 conflict contract and staged/atomic replacement | `cli/commands/skill.py`, `plugins.py`, `mcp.py` |
| Atomic apply patterns | stage `.<name>.new` → move `.trash`; `_atomic_plugin_replace`; dot-prefixed residue ignored by discovery | `_converge_skills.py`, `plugins.py`, `shared/plugins_config.py` |
| Supply-chain gate | `shared/packages/skills/skill_scan.py` on every ingest (critical → refuse, `--accept-risk` recorded, trust never auto-promoted) | `shared/packages/skills/skill_scan.py`, `shared/install_registry.py` |
| Manifest + host-compat gate | `ava-plugin.json` validator, range algebra, `engines.ava` vs the checkout's `pyproject.toml` version | `shared/plugin_manifest.py`, `conventions/plugin-spec-v2.md` |
| Per-machine OS jobs | launchd / crontab / schtasks registrars, idempotent, converge-registered (health probe, watchdog, autostart, logs), test switch `AVA_OS_JOBS_ENABLED=false` | `shared/os_*.py`, `cli/commands/_converge_os_jobs.py` |
| Cluster extension registry (S2, in progress) | `extensions` / `extension_blobs` tables; install writes row+blob; converge/boot materialize; adoption sweep; content-addressed by tree hash; trust rises only | `shared/extension_registry.py`, `shared/extension_materialize.py` |
| Update coordination | cluster-wide DB update lock + in-flight detection; source-tree tamper detection (health-probe check 8, alert-only) | `shared/cluster_lock.py`, `cli/commands/status.py:_update_in_flight`, `shared/source_tree_guard.py` |
| Existing boundaries | four-layer modification model; extension ownership (cluster/machine/agent); CLI scope convention; CLI-only updates | `decisions/2026-08-19-four-layer-modification-model.md`, `decisions/2026-08-21-extension-ownership-three-tiers.md`, `2026-08-02-cli-scope-convention.md`, `2026-08-05-cli-only-updates.md` |

## 3. Constraints the design must respect (hard facts)

1. **Skills are hot; plugins are process-bound.** Skill content takes effect on the next scan/read. Plugin code (agent-side) takes effect at agent-process start (`load_extensions()` at graph build, `agent/_extensions.py`); provider plugins (`provider.py`) load once per process, lazily, in *every* process that builds or validates a chat model — agent, gateway, labeler, eval harness. There is no in-process reload surface today.
2. **The prod checkout must stay clean.** On a source-run home the health probe alerts on changed tracked files and untracked files outside the runtime-artifact whitelist (`shared/source_tree_guard.py`). Derived/runtime content must therefore live OUTSIDE the checkout — in `$AVA_HOME` data dirs — or it raises a permanent tamper alert (or forces a permanent whitelist exception).
3. **Rollouts own the code channel.** `ava cluster update` (CLI/operator only; agents cannot trigger it — ruling 2026-08-05) is the only path that moves code, schema, and services. Content refresh must be a *distinct, non-service-touching* pass — never a rollout, never a service restart.
4. **Installs are per-machine today; ownership is moving cluster-side** (`decisions/2026-08-21`, issue #39, S2 landed for skills). The design must work per-machine now and migrate cleanly to cluster rows later (policy as data, not as machine-local law).
5. **Everything ingestible passes the scan gate and never auto-promotes trust.** Auto-update must keep this intact — no `--accept-risk` override in automatic paths.
6. **Repo doc/CLI conventions**: updates stay reachable via the CLI (top-level `packages` namespace fits the convention); docs are English-only; per-machine verbs are top-level without a scope marker.
7. **Disk discipline.** Any mirror/rollback retention must be bounded (the fleet runs on pet machines; macmini disk discipline is an explicit standing rule).
8. **Plugin load context — fixed 2026-09-11; the history still constrains the design.** The fleet took two outages from *asymmetric loader contracts* (2026-08-28: host boot exec'd plugin.py under a top-level name, so a relative import crashed every `import ava`; 2026-09-10/11: the boot loader ignored the enable set, and a hand-placed plugin stopped agents from starting). As of 2026-09-11 (task #2985, PRs #2201, #2206) there is **one loader contract** — boot and graph build share `load_plugin_module` / `safe_load_plugin_module`; external plugins get their `plugins.<name>` namespace chain registered so relative imports resolve; every load site is **fail-soft** (a broken plugin is skipped with a loud `plugin_load_failed` report, and the host survives). What stays **fail-closed** is inventory/contract conflict: duplicate plugin names, malformed config, schema drift, provider registration-contract violations. The design must respect both halves: no new load path without the shared primitives, and no candidate tree entering a live load path without an isolated probe.
9. **Plugin development: paused overnight, resumed 2026-09-11 10:06.** The 09-10 night incident (an unverified plugin written into the production plugins root (`$AVA_HOME/plugins/`) stopped agents from starting) triggered a pause; the user's 10:06 order lifted it, paired with the fail-soft hard mandate. The standing red line stays: verify in isolation, never hand-place code into a production load path — every candidate goes through staging + probe + atomic swap.

## 4. Candidate mechanisms

Comparison axes: coupling boundary / versioning / update atomicity / failure & rollback / migration cost.

### A. Git submodule (content repo mounted inside the main repo)

- **Coupling boundary**: content repo separate; superproject pins one commit (the gitlink). Kernel+content co-changes become two commits across two repos (content first, then a gitlink bump) — reviewable together only by reference.
- **Versioning**: git commits/tags; the gitlink is the shipped pairing. Runtime "floating" (submodule tracking its branch) is *not* versioned anywhere — it exists only as a dirty working tree.
- **Update atomicity**: one submodule = one content set moves together (coarse). Multiple submodules = per-group granularity at the cost of more machinery.
- **Failure & rollback**: git handles per-submodule; but the runtime state fights two existing invariants: the source-tree guard alerts on a dirty checkout (a floating submodule shows as permanently dirty, or must be whitelisted into the invariant), and `ava cluster update` rewrites the tree (`checkout --force -B main <sha>`) with a submodule story that must be designed into every leg. Silent-revert risk: running `git submodule update` anywhere in the update flow would silently undo channel-applied content.
- **Migration cost**: high — contributor workflow, CI checkout config, install/bootstrap, guard exceptions, docs.
- **Verdict: not recommended as the runtime mechanism.** The genuine value (a pinned default pairing visible in the superproject) is obtainable with a lock file; the costs are structural and land inside invariants we just spent months hardening.

### B. Independent content repo(s), consumed from a data-dir clone

- **Coupling boundary**: explicit cross-repo contract. Core content is authored in its own repo; CI checks it against a pinned kernel ref; every executable package declares `engines.ava`. Kernel+content changes need two PRs (sequenced), with the pin making the pairing explicit. Note: this *weakens* R1's convenience — SDK-internal references still work, but co-landing stops being atomic.
- **Versioning**: independent tags/releases for content; per-package manifests; a machine tracks a channel ref (main, or a release train). Per-package applied revision recorded on the machine.
- **Update atomicity**: fetch into a data-dir clone/mirror; per-package staged swap (or set-atomic apply across packages).
- **Failure & rollback**: content repo history/tags are the rollback source; keep previous package tree (N-1) + applied revision. Clean, because nothing lives in the checkout.
- **Migration cost**: highest — history split (subtree), CI, docs/links, install seed, two-repo contributor flow, and the "where do I send a fix for skill X" question.
- **Verdict: the right long-term shape** when content needs its own release train, external publication, or non-maintainer contributors. Overkill now; but the channel abstraction must make it a *source swap*.

### C. Extend the existing packages flow (content stays in the main repo; channel = its remote, extracted by path)

- **Coupling boundary**: maximal — one repo, one PR/CI; R1 fully served (content can reference internals because it *is* the repo).
- **Versioning**: content rides the repo's ref (main today; a dedicated content ref/tag train can be added later without changing the mechanism). Per-package applied revision + hash recorded in the registry; `engines.ava` gates plugins; skills are fail-soft.
- **Update atomicity**: `git fetch` (objects only — never touches the checkout tree) + `git archive <rev> -- <paths> | tar` into a per-package staging dir + staged swap. Per-package atomic; optionally group-atomic.
- **Failure & rollback**: keep the previous tree + applied revision; re-apply the previous revision to roll back; git history is the source of truth for "what did we run last week".
- **Migration cost**: smallest — reuses the existing discovery, scan and land code paths (the load-dir sync contracts, the conflict harness, plugin atomic replace). New pieces: the channel fetcher, policy fields, the executor, and (P2) the plugin root materialization.
- **Risks**: content may run ahead of the installed code (accepted and bounded below); no separate content release train until one is added.

### D. Hybrid — C for delivery now, B-shaped abstraction, one policy plane for all package kinds

- Core channel = C (fetch repo content paths, apply from a data dir).
- Third-party git packages = the existing per-package flow, extended with the same policy/executor (`auto`/`notify`/`off` + interval), reusing `acquire_source` + conflict guards.
- The channel source is a configurable value, so a future move of core content to its own repo (B) — or a lock-pinned seed — is a source swap, not a rewrite.
- **Verdict: recommended.**

### Comparison matrix

| | A submodule | B content repo | C extend existing flow | D hybrid |
|---|---|---|---|---|
| Coupling boundary | separate repo, pinned gitlink | separate repo, explicit pin | one repo (max coupling) | C now, B-portable |
| Versioning | git + gitlink; floating state unversioned | independent tags/trains | repo ref + per-package applied rev | both, per channel |
| Update atomicity | per submodule (coarse) | per package / set-atomic | per package / set-atomic | per package |
| Failure & rollback | git; risks invariant fights | clean (data dir) | clean (data dir) | clean |
| Migration cost | high (workflow + invariants) | highest | smallest | small now, staged later |
| Verdict | not as runtime mechanism | evolution path | POC start | **recommended** |

## 5. Recommended architecture

### 5.1 Concepts

Four concepts, each with one job:

1. **Package** — one tracked directory with a kind (`skill` / `plugin` / `mcp`). Unchanged: the install registry row is the identity, `content_hash`/`installed_hash` are the edit guards.
2. **Channel** — where new content for a package comes from. Two channel kinds cover everything:
   - `core` — repo content paths (`ava_builtins/skills/<name>/`, `ava_builtins/plugins/<p>/skills/`, and in P2 `ava_builtins/plugins/<p>/`), fetched from the repo remote by ref. One channel state per machine (last seen head, last fetch, last result).
   - `git` — the package's recorded `source` + `ref` (the existing install flow). `ls-remote` is the cheap check; re-fetch happens only when the ref moved.
   Local-path and hand-registered packages have no channel and are skipped (as today).
3. **Update policy** — per package: `mode` ∈ {`auto`, `notify`, `off`} + `check_interval_seconds`. `auto` = check and apply; `notify` = check and record "update available", never apply; `off` = never checked, never applied (explicit verbs still work). Defaults by source class, overridable at install and later.
4. **Refresh pass** — the one executor that walks due packages and performs check → gate → apply → record. Idempotent, safe to run at any time, concurrency-guarded, bounded.

Vocabulary note: "content refresh" is deliberately NOT called an update. `ava cluster update` remains the only *code* update; refresh never touches code, schema, services, or the checkout tree.

### 5.2 Data model (install registry, schema v2)

One schema bump; the existing `Registry.version` field is the migration anchor:

```python
class UpdateState(BaseModel):
    mode: Literal["auto", "notify", "off"] = "off"   # resolved default written at first sight
    interval_seconds: int | None = None               # None = kind/source default from settings
    channel: str | None = None                        # "core" | "git" | None (local)
    applied_rev: str | None = None                    # commit SHA (core) / ref@sha (git) of what is on disk
    last_check_at: str | None = None
    last_apply_at: str | None = None
    last_result: str | None = None                    # up_to_date | applied | available | conflict | error: ...

class ChannelState(BaseModel):
    name: str                                         # "core"
    remote_url: str
    ref: str                                          # default "main"
    last_seen_sha: str | None = None
    last_checked_at: str | None = None
    last_result: str | None = None

class Registry(BaseModel):
    version: int = 2
    packages: list[InstalledPackage] = []
    channels: dict[str, ChannelState] = {}
```

On load of a v1 file: rows get `update = UpdateState(...)`, and the file is rewritten lazily on the next write (later retired, batch b5 2026-09-20: the reader now demands exactly v2 and refuses v1). `applied_rev` for legacy rows is inferred at first refresh: the checkout's installed commit for repo-origin content, the recorded `ref` for git packages.

Defaults (user ruling 2026-09-11 15:25 — one cadence for everything):

| Package class | mode | interval |
|---|---|---|
| every channel-backed package (core; git-sourced skill/plugin/mcp) | `auto` | **24h** |
| core plugins (P2; materialized) | same, with activation kept at process boundaries (§5.4) | 24h |
| plugin-carried skills | inherit the parent plugin's row | — |
| local / hand-registered | `off` (no channel) | — |

Manual refresh is always available regardless of cadence: `ava packages refresh` (all or one package) plus the existing per-package verbs; per-package `notify` / `off` remain as overrides (install flags or `ava packages policy <name>`). The core channel's `ref` default is `main` (user ruling — merge = publish), and refresh is ON by default per that same ruling.

### 5.3 The refresh pass (`ava packages refresh`)

Algorithm, per run:

```
skip unless: os jobs enabled (when run from the job)
        and no refresh run is already active (per-home flock)
        and no cluster update is in flight (reuse _update_in_flight() + gateway orchestration session)
        and this host's registry is readable

for each channel with any due package:
    core: git -C <checkout> ls-remote <remote> <ref>   -> head_sha        (cheap; no clone)
          if head_sha == last_seen_sha and no package is retrying: done
          git -C <checkout> fetch <remote> <ref>                            (objects only; tree untouched)
    git:  git ls-remote <source> <ref>               -> head_sha per package

for each due package (auto|notify), oldest-check-first, bounded (budget: e.g. 60s, ≤ N applies):
    determine remote_rev (channel head / ls-remote result)
    if remote_rev == applied_rev: record up_to_date; next check = now + interval
    else:
        stage: extract/acquire new content into $AVA_HOME/…/.staging-<name>/
               core: git archive <head_sha> -- <paths> | tar -x -C <staging>
               git:  acquire_source(source, ref) (existing code path)
        gates (any failure -> keep disk as-is, record the outcome, backoff; never a forced overwrite):
            - trees non-empty, expected entry exists (SKILL.md / plugin.py | .claude-plugin/plugin.json)
            - supply-chain scan (shared/packages/skills/skill_scan.py) — critical finding refuses; NO auto --accept-risk
            - plugin manifest validation (existing code)
            - version gate (§5.5): engines.ava must include this host's version — else record
              blocked_version, keep the current content, retry after the host moves
            - local-edit guard: on-disk tree hash must equal the recorded hash
              (a hand-edited copy is NEVER overwritten — same contract as `ava skill update`)
        apply (atomic, in this order):
            - skills: staged swap into $AVA_HOME/skills/<name>/ (stage → move old to .trash → move new in)
            - plugins (P2): staged swap into $AVA_HOME/plugins/<name>/ (the standard external root; existing _atomic_plugin_replace pattern)
        outcomes: applied | available | blocked_version | conflict | refused_scan | error
        record: applied_rev, content_hash/installed_hash, updated_at, last_result
    for notify packages: stop after the check decision — record "available" with the remote rev (no staging, no apply)

report: one summary line per run + per-package state in the registry (log + `ava packages status`)
```

`ava packages rollback <name>` restores the retained previous tree (`.prev`) — the manual escape hatch for a bad update and the recovery path when the host has moved past a package's max bound. The refresh pass itself never auto-rolls-back.

Design decisions embedded above:

- **Check ≠ fetch.** `ls-remote` answers "did the ref move" over the network without cloning; a full fetch happens only when it did. The core channel's fetch goes through the existing checkout's git, objects-only — the working tree, HEAD, and the source-tree guard are untouched. This is an established contract, not a new risk: `shared/cluster_drift.py:prod_source_fetch` already fetches refs into the prod checkout ("writes to the object store and FETCH_HEAD — never the working tree") for pin-ancestry checks, with a bounded timeout; the channel fetch should reuse that pattern. A future wheel-mode deployment (no checkout) uses a data-dir bare mirror instead — same interface.
- **Per-package granularity with a per-channel head.** The channel's head commit is a fleet-wide fact; which *packages* changed between `applied_rev` and the head is computed with `git diff --name-only <applied_rev> <head> -- <path>`, so a skill edit touches one package.
- **Never auto-overwrite a human.** The local-edit guard is the existing R5 contract, reused verbatim; conflicts are recorded, not forced. `--force` remains a human-only flag.
- **Bounded and polite.** Per-run wall budget, apply cap, network timeouts, ±jitter on intervals, exponential backoff on repeated errors (recorded in `last_result`).
- **Removal is out of scope.** The pass never deletes packages; a package whose source disappeared upstream is left as-is — removal belongs to the converge cleanup path.
- **Never a rollout.** The pass takes the per-home refresh flock and refuses while an update is in flight; it never restarts a service, never writes the checkout, never touches the DB schema. It may run while the cluster is fully live — that is the point.

### 5.4 Activation boundaries (the "no disruption" contract)

| Content | Where it lands | When it activates | Who can force it sooner |
|---|---|---|---|
| Skill (any origin) | `$AVA_HOME/skills/<name>/` | next skill scan / `ava.help()` read — effectively immediate, always hot | — (nothing to force) |
| Agent-side plugin (core, P2) | `$AVA_HOME/plugins/<name>/` — the standard external root (managed shadowing; details in §6 P2) | next agent-process start (spawn / restart) |
| Third-party installed plugin | `$AVA_HOME/plugins/<name>/` (unchanged) | next agent-process start | operator/agent: existing restart verbs (`ava.agents.restart`), staggered and turn-boundary-safe by construction |
| Provider plugin (`provider.py`) | same | next process start of *every* model-building process (gateway, agent, labeler, eval) | next gateway restart — an operator action (rollout or explicit) |
| MCP server | `$AVA_HOME/mcps/<name>/` | next connect | — |

Rules:
1. **The refresh pass never restarts anything.** Content lands; activation is deferred. This is the reason the 24h cadence cannot disturb the cluster.
2. **Pending activation is a first-class, visible state**: `ava packages status` shows "landed <rev>, activates at next restart" so a "did my update take effect?" question has one place to look.
3. **Ruled (user, 2026-09-13): natural boundaries only.** The refresh never restarts anything; the opt-in `refresh.activate_idle_agents` idea stays unimplemented unless asked for later.
4. Provider-plugin activation stays operator-timed (gateway restarts bounce sessions/schedules; that cost is the operator's call, not a background job's).

### 5.5 Versioning & compatibility model

*(Added 2026-09-11: a package may need a very new Ava to load at all; skills and plugins may declare both a minimum and a maximum host version.)*

**Axes (v3 — commit-date axis, chosen over a hand-maintained `pyproject` number; see the decision record).**
- **Host version = derived from the commit, never hand-maintained.** The version the gates compare against is `YYYY.M.D` (commit date of the running build; display adds the short SHA, e.g. `2026.9.11+gabc1234`). It always exists on every machine (the commit identity is already recorded — `installed_sha` / `running_sha`), it advances automatically, and it needs no release process and no bump discipline. Assessment of the alternatives:
  - *Static `pyproject.toml` `[project].version`* (v2's anchor) — vestigial in this repo: `0.1.5`, frozen since the initial public release, read by nothing in the deploy path (rollouts pin commits). Rejected as the gate source.
  - *The dated release tags* (`vX.Y.Z-YYYYMMDD[-HHMM]`, cut by `scripts/release_cut.py`, parsed by `shared/release_tags.py`, usable as the rollout target via `AVA_TRACK_MODE=releases`) — the intended long-term human-facing release identity, and the same *date* axis this model uses. But the cadence is currently dormant (last dated tag 2026-08-08; nothing pushed to origin — Ava's recent releases have been manual milestones v0.2…v0.7). So the gate must not depend on it: commit date is always derived, and when a dated release exists its date agrees with the commit date by construction.
  - *Pure calendar version in `pyproject`* (e.g. `2026.9.11`) — mechanically fine, but it re-introduces the hand-maintenance rejected above; the derived form needs no file edit at all.
- **Precision layer — commit ancestry.** Anything that must be exact ("needs the commit that added X") declares `requires_commit: "<sha>"`; the host passes iff its commit contains it (`git merge-base --is-ancestor` against the recorded SHA). Zero discipline, exact within a day. Day-level date granularity plus this layer covers everything: text (skills) can live on the date axis; executable content (plugins) uses both.
- **Package version** — the semver `version` in a package manifest; recorded per package for status and rollback reasoning (core content may set it to the date of its last change; not load-bearing).
- **`engines.ava`** — a range (`>=A,<B`; min **and** max supported by the existing range algebra; either side may be unbounded), compared against the derived host version; plus the optional `requires_commit` (ancestor check). When a manifest exists, the validator already requires `engines` — implementation note: relax that to "engines or requires_commit" so core content can be commit-pinned only.

**Declaration per kind.**
- Plugins (any origin): `ava-plugin.json` (`version`, `engines.ava`) — already implemented.
- Skills: an **optional manifest beside SKILL.md** (same `ava-plugin.json` format; for a bare skill package only `apiVersion` / `name` / `version` / `engines` are used). One format, one validator, one range algebra. Rejected alternative: SKILL.md frontmatter fields — a second parsing path, YAML quoting hazards the repo has already been bitten by, and no package-level identity. A plugin bundle's range governs its bundled skills (one source of truth).
- Core content: manifests are added where a constraint matters (not blanket for every skill); CI keeps them honest (below).

**Enforcement points.**

| Point | Check | On violation |
|---|---|---|
| install / upgrade (`ava skill install`, `ava plugins install\|upgrade`, `ava mcp install\|upgrade`) | existing `check_host_engine` | refuse, report (unchanged) |
| refresh landing (§5.3) | range vs host version | keep current content; record `blocked_version: requires ava <range>, host <v>`; retry after the host moves; visible in status |
| runtime load — plugins | range vs host version | skip that plugin + loud error + status entry; the process continues. Same containment the loader now gives every plugin-code failure (§3.8) — a declared mismatch is an expected state, never a process failure |
| runtime scan — skills | range vs host version | excluded from the catalog/index with a status reason; a blocked refresh leaves the previous content untouched |

**No bump discipline (v2's proposal retired).** v2 asked authors to bump `[project].version` on content-facing merges, enforced by CI in both directions. That was over-engineered for a number nothing else maintains — dropped. In its place:
- Core content that depends on a fresh kernel capability declares `requires_commit` (the merge that introduced it) — mechanical, no bump, no CI-side bookkeeping.
- Date-axis ranges (`engines.ava`) remain for third-party content, which targets *released* Ava versions; they compare against the derived host version.
- CI keeps one check: a declared `requires_commit` must be an ancestor of the merge (a typo'd or future SHA is red).
- Separate observation (flagged 2026-09-11): the dated release pipeline (`release_cut.py` daily/weekly + `AVA_TRACK_MODE=releases`) is dormant. If/when it is revived, released commits gain a first-class human version and third-party ranges get their natural reference points — that is a release-process decision, not a dependency of this design.

**Edge cases.**
- Host rollback (the version moves backwards): the load gates catch it; status explains; `ava packages rollback` recovers content.
- Blocked refreshes stay visibly blocked in `ava packages status`; they unblock automatically when the host updates.
- Unresolvable `requires_commit` (the required object is not present locally) is a **gate failure**: record `blocked_version`, keep the current content, and retry on a later refresh — never a pass, never an error.
- Max-bound staleness: past-max content is disabled loudly at load; core content gets fixed via CI, third-party content via its maintainer.
- Unversioned legacy content: no gates, unchanged behavior.

**Trust (unchanged by versioning).**
- Core content stays `builtin`; a refreshed third-party package keeps its tier (a changed adopted cluster row resets to `unreviewed` per S2 semantics); auto paths never promote trust and never pass `--accept-risk`.
- Third-party auto-update is the default per the user ruling (24h); the scan gate, the trust rules and the conflict guard are what keep that safe, and per-package `notify`/`off` overrides remain.
- A `ref` pinned to a tag or commit stays pinned: the channel never advances a pinned ref.

### 5.6 Executor, scheduling, and surfaces

- **Executor**: one CLI verb, `ava packages refresh [--check] [--package NAME] [--json]` — the same code path for manual runs, the OS job, and (later) a gateway-triggered host pass. `--check` = check-only.
- **Scheduler**: one OS job per machine, registered by converge exactly like the existing ones (new module `shared/os_packages.py` → platform backend; idempotent; `AVA_OS_JOBS_ENABLED=false` respected; Windows degrades to a loud warning like its siblings). Base tick every 15 min; per-package intervals are data, so the tick only bounds granularity — one job, no job churn when policies change. Rationale for per-machine: installs are per-machine today, each machine has its own load dirs, and the OS job is the established, service-independent primitive (it fires whether or not the gateway is up).
- **Status surface**: `ava packages status` — per package: kind, origin, channel, policy, applied rev, last check/apply result, pending update, and next check; plus channel line (core@main head + last fetch). A machine-readable `--json` for the console later.
- **Observability**: registry fields + log lines (existing `shared.log`); optional dedicated events (`package_refresh_applied` etc. into the event registry) in a later slice — not POC.
- **Frontend** (later): the existing Skills/Plugins panel sections gain "update available / auto / interval" per row, driven by `--json`.

### 5.7 Interaction with existing flows (the traps)

1. **Rollout's builtin refresh must skip channel-managed packages.** `_update_local._refresh_builtin_skills` / `_update_agent_runner...` run `ava skill update` after a code pull. If a machine's copy is channel-managed (applied from a newer content rev than the checkout's), that leg would *downgrade* it. Rule: `ava skill update` (and its rollout legs) applies checkout content only to packages whose channel is not `core`, or whose policy is `off`; channel-managed packages are refreshed by the channel. (Alternative considered and rejected: ancestry comparison — "apply checkout content only if it is newer by git ancestry" — clever, untestable at fleet scale, and violates Keep-It-Simple.)
2. **Converge seeding stays as-is (bootstrap-only)** for machines that have never channeled: the checkout copy remains the seed for fresh installs and for wheel-mode. Once a package is channel-managed, the channel is the writer.
3. **The source-tree guard is untouched.** Content lives in `$AVA_HOME` data dirs; the checkout is never a content destination. This is also why the submodule candidate is rejected.
4. **S2 cluster rows (issue #39) are the migration target, not a competitor.** P1 keeps machine-local channel state (works today, zero DB dependency). When S4 lands (plugins as cluster rows), the policy fields become cluster columns and the machine executor only materializes + fetches locally; the refresh pass keeps its interface ("make this machine match the policy").
5. **Wheel-mode (`prepared updates` / retained generations)**: `external_plugin_read_root()` already resolves to the generation's read-only plugins dir there; the materialization semantics for that mode belong to the prepared-update machinery (`shared/runtime_plugins.ava.okf.md`). P2 targets checkout-mode deployments; the channel/policy model feeds that machinery later rather than duplicating its roots.
6. **Dev worktree clusters**: OS job registration is per-home; dev homes follow their existing converge behavior (and tests switch jobs off globally).
7. **Disk**: bounded retention — keep exactly one previous tree per package for rollback (`$AVA_HOME/…/.<name>.prev`, dot-prefixed and discovery-ignored), prune on next successful apply; no unbounded mirror growth (objects fetched into `.git` are the checkout's own repo growth, managed by existing gc).

### 5.8 Evolution (when the pressure appears)

- If content needs its own release train, tags, or external publication: **move the content paths to their own repo (B) and point the `core` channel's source at it** — the refresh executor, policy plane, and application machinery are unchanged. The repo lock file (pinned default rev for fresh installs) is the submodule-pin equivalent.
- If per-agent or canary versions become a requirement — explicitly out of scope (S1 decision: side-by-side versions are a non-goal).

## 6. Migration path and POC scope

Each phase is independently landable and reversible; nothing in P0/P1 changes code-update behavior.

### P0 — schema and read-only surface (no behavior change) — **landed: PR #2355**
- Registry schema v2: `UpdateState` / `ChannelState` fields, lazy migration (retired, batch b5 2026-09-20: a v1 file is refused), defaults resolution from settings (`shared/config/packages.py`: per-class default mode/interval, base tick, master switch).
- `ava packages status` (read-only) + `--json` — including the host version and each package's declared range (§5.5).
- Version plumbing: optional manifest support for skill packages; the core-content CI check (declared ranges must include the repo's current version); the derived host-version policy recorded in [`conventions/host-versioning.md`](../../conventions/host-versioning.md) (no bump discipline; `[project].version` remains only as the wheel-mode fallback).
- Docs: the ruling entry + this elaboration (landed together); update `okf/skills/load-directory-sync.ava.okf.md`, `cli/commands/packages/packages.ava.okf.md`, and the `ava-modification-layers` / `develop-a-plugin` skill phrasing ('kernel-shipped base set, changed via L4') when P1/P2 land.
- Acceptance at landing: v1 file loads, migrates on next write, defaults visible in status; no behavior change elsewhere (test lock: registry round-trip + migration) — the v1 leg later retired, batch b5 2026-09-20: v1 files are refused.

### P1 — skills fast lane (the POC; the deliverable the user can feel) — **landed: PR #2368**
- `ava packages refresh` implementing §5.3 for `skill` packages, `core` channel first (fetch from the checkout's remote, per-package diff, archive-extract, gates, staged swap, records) and `git` channel second (reusing `acquire_source` / `cmd_skill_upgrade` semantics).
- OS job registration (`shared/os_packages.py`, new module; 15-min tick, converge step) behind a setting; default ON for skill-class core content after the user's ruling; OFF until then (safe rollout).
- Rollout skip rule (§5.7-1) in the two `_refresh_builtin_skills` legs + `cmd_skill_update`.
- Install flags: `ava skill install ... [--update-mode auto|notify|off] [--check-every <dur>]` (default auto/24h per the user ruling).
- Version gates wired into the refresh (§5.5) + the runtime skill filter (out-of-range skills excluded from the catalog with a status reason) + `ava packages rollback <name>`.
- Acceptance (end-to-end on macmini, then a second machine):
  1. merge a skill-only PR to main → within the interval, the machine's load dir shows the new content, `applied_rev` = the merge commit, no service restarted (`ava status` shows nothing bounced), and an agent reads the new body via `ava.help`.
  2. hand-edit a load-dir copy → the next refresh refuses with a conflict record, content preserved.
  3. offline / fetch failure → old content stays, `last_result` records the error, backoff, cluster unaffected.
  4. refresh during an in-flight `ava cluster update` → skipped (recorded).
  5. `notify` package → a moved ref records "available" without applying.
  6. idempotence: consecutive runs apply nothing, checks are cheap (`ls-remote` only).
  7. no checkout mutation: `git status` in the prod checkout unchanged after refresh (source-tree guard agrees).
  8. version gate: a package whose `engines.ava` excludes this host is recorded `blocked_version` and keeps its previous content; when the host version moves past a package's max, its skill copy is dropped from the catalog with a visible reason.
  9. manual refresh works on demand and reports the same outcomes.
- Test locks: unit (due math, policy resolution, diff→package mapping, gate order, conflict guard, backoff, flock), integration (local git fixture: two commits, one package changed), and the S2 two-home chain remains green.

### P2 — core plugins (structural; the pause lifted 2026-09-11 — schedulable, with the isolation gates below)
- **Updated against the loader work landed 2026-09-11 (task #2985).** v2 of this section assumed a special "package-context root" was required; that is no longer true — the standard external root resolves package-relative imports exactly like the builtin tree (one loader contract), and failures are contained. P2 gets simpler, not riskier.
- Remaining work and decisions:
  (a) **Collision rule** — a materialized copy of a builtin name is today a fail-closed duplicate. Either adopt **managed shadowing** (a registry-tracked materialized copy wins over the builtin; untracked collisions stay fail-closed) or move the core set out of the builtin tree (bigger migration). Recommendation: managed shadowing, locked by a test.
  (b) **Module identity change** (`ava_builtins.plugins.<n>` → `plugins.<n>`): verify and lock every surface that observes it — checkpoints/plugin state, attribution ledger, provider loading, ops-service discovery, `ava plugins inspect`.
  (c) **Landing + activation**: reuse what now exists — atomic materialization (`_atomic_plugin_replace` + the #2985 atomic install), the release probe (`_release_plugin_probe`), then boundary activation per §5.4 (land-only; provider plugins staged until the next gateway restart).
- POC scope: ONE agent-side core plugin end-to-end (suggest `ava_syntax_fix` — no services, no provider binding), then one provider plugin (`lm_deepseek`) staged-only. Verify ops-service discovery for a materialized tree in checkout mode (ops roster/inventory reads repo dirs today — item to check).
- Ordering: schedulable now (the development pause lifted 2026-09-11 10:06) — and never by hand: every candidate tree is staged, probed and swapped by the same machinery (the 09-10 incident was hand-placed code in a load path).
- Acceptance: a plugin-only PR merged to main → tree lands in the external root within the interval; the next agent process start runs the new code (assert via a versioned log line); no restarts performed by the refresh; `ava plugins inspect` shows the loaded path; unmanaged collision still fails closed; a deliberately broken candidate is refused by the probe and never reaches the load path.

### P3 — alignment with extension ownership (issue #39, S4/S5)
- Policy fields move to cluster rows (`extensions.update_*`), machine executor reads policy + materializes; the source of truth for "this cluster's core content rev" becomes a cluster column (fetched once per cluster where the gateway can reach the remote).
- Optional: `core` content as a distinct publication artifact (repo tag `content-YYYY.MM.DD`, release asset) for wheel-mode installs.

## 7. Risks, and what holds them

| Risk | Mitigation |
|---|---|
| Content ahead of installed code breaks a *skill* | fail-soft by design; visible in transcript; CI on the repo checks content against main; escalate normally |
| Content ahead of code breaks a *plugin* | `engines.ava` hard gate at every landing; refuse + retry later |
| Auto-applied third-party content | **user-ruled default (24h auto)**; the scan gate, trust rules (no promotion, no auto `--accept-risk`) and the conflict guard are what keep it safe; per-package `notify`/`off` overrides |
| Refresh races a rollout / the source-tree guard | per-home refresh flock; skip when update in flight; content never lives in the checkout; rollout legs skip channel-managed packages |
| A skill edit merged to main is *not* cluster-reviewed for the fleet | it is reviewed by the repo's PR/CI (same as any commit); the fast lane changes delivery, not review |
| Disk growth (previous trees, fetched objects) | one previous tree per package (pruned on next apply); no mirrors in P1; wheel-mode mirrors get the same bound |
| Core-plugin materialization changes module identity (import context is now uniform) | P2 verifies/locks the identity-observing surfaces (§6 P2b) + probe + atomic swap before any live path; isolation gates §3.9 |
| Version gates go stale — a host upgrade strands content, blocked refreshes pile up | keep-old + visible `blocked_version` state + the core-content CI check; `ava packages rollback` recovers the moved-past-max case |
| Complexity creep | one channel concept, one executor verb, one job, policy as data; explicit non-goals below; reuse of existing contracts wherever possible |
| Windows | job registration degrades to warning (existing backend behavior); atomic dir swap caveats checked in P2 (files are not held open after import) |
| Silent drift of the *policy* itself (e.g. defaults changed by an update) | defaults live in settings, written into rows at first sight; status shows resolved values |

## 8. Explicit non-goals

- No in-process plugin hot reload (agent or provider) — activation stays at process boundaries.
- No per-agent / side-by-side content versions (S1 decision stands).
- No auto-`--accept-risk`, no trust promotion, no bypass of the scan gate.
- No changes to `ava cluster update` semantics or cadence; the code channel is untouched.
- No marketplace/publication effort; a content repo split is an evolution path, not part of this work.
- No automatic service restarts for content activation (an opt-in idle-agent activation is presented as an open question, not a default).

## 9. Decisions and defaults

**All three decision points ruled 2026-09-13 (option A each; recorded in [`decisions/2026-09-13-core-package-update-channel.md`](../../decisions/2026-09-13-core-package-update-channel.md)):**
1. **Activation** = natural process boundaries only (§5.4 rule 3).
2. **Host version** = commit-date-derived + `requires_commit` precision layer (§5.5 v3). The dormant dated-release pipeline stays out of this design — reviving it remains a separate candidate work item.
3. **P2 timing** = scheduled after P1.

**Defaults adopted** (flagged 2026-09-11; change on request): skill manifests are optional and used where a constraint matters; core content stays in this repository; blocked packages surface in status/logs only for v1.

## Appendix A — current-state evidence (for reviewers)

- Load dir sync + R5 bootstrap-only: `cli/commands/_converge_skills.py` docstring; `okf/skills/load-directory-sync.ava.okf.md`.
- Explicit update verbs + conflicts: `cli/commands/skill.py` (`cmd_skill_update` L423+, `cmd_skill_upgrade` L510+), `cli/commands/plugins.py` (`cmd_plugins_upgrade` L390+), `cli/commands/mcp.py`.
- Rollout skill refresh legs: `cli/commands/_update_local.py:85` (`_refresh_builtin_skills`), `cli/commands/_update_agent_runner.py:250`.
- Registry model: `shared/install_registry.py` (`InstalledPackage`, `Registry.version`, `tree_hash`, `copy_changed`).
- Plugin discovery + loaders: `shared/plugins_config.py:_discover_plugins`, `agent/_extensions.py:load_extensions`, `shared/lm/_plugin_providers.py`; roots: `shared/paths.py:repo_plugins_dir/plugins_dir`, `shared/runtime_interpreter.py:external_plugin_read_root`.
- Manifest/engines gate: `shared/plugin_manifest.py` (`host_version_from_repo`, `check_host_engine`), `conventions/plugin-spec-v2.md`.
- OS jobs: `shared/os_cron.py` (5-min health tick as the registrar template), `cli/commands/_converge_os_jobs.py`, `AVA_OS_JOBS_ENABLED`.
- Update coordination: `shared/cluster_lock.py`, `cli/commands/status.py:_update_in_flight` L58, `shared/source_tree_guard.py` (reset --hard + clean -fd; skip while update in flight); objects-only fetch precedent: `shared/cluster_drift.py:prod_source_fetch`.
- Extension ownership S1/S2: `decisions/2026-08-21-extension-ownership-three-tiers.md`, `future/infra/extension-ownership.md`, `shared/extension_registry.py`, `shared/extension_materialize.py`.
- Four-layer model / builtin-plugin ruling: `decisions/2026-08-19-four-layer-modification-model.md` (revised in part: builtin plugins stay *authored* in the kernel but are *delivered* via the content channel).
- Historical incident class: skill edit merged to main, runtime stale for two days (2026-08-27). R5 background: task #1013.
- Plugin load-context incidents (2026-08-28: a relative import crashed under a top-level exec; 2026-09-10: a hand-placed plugin stopped agent starts). The pause they triggered was lifted 2026-09-11 10:06; the standing red line: never hand-place code into a production load path.
- Loader unification (the current contract): `okf/plugins/module-loading/module-loading.ava.okf.md` + `okf/plugins/module-loading/fail-closed-boundaries.ava.okf.md`; task #2985 (done 2026-09-11; PRs #2201, #2206) — one contract for boot + graph build, `plugins.<name>` namespace chain for external plugins, fail-soft containment on every load site.
- Host version evidence: `pyproject.toml` `[project].version = 0.1.5`, set at the initial public release and never changed since (deploys pin commits; nothing reads it in the deploy path). Release machinery that exists but is dormant: `scripts/release_cut.py` (dated tags `vX.Y.Z-YYYYMMDD[-HHMM]`, daily/weekly), `shared/release_tags.py`, `AVA_TRACK_MODE=releases` in `cli/commands/_update_git.py` + `shared/config/general.py` — last dated tag 2026-08-08; origin carries no dated tags; recent releases were manual milestones (v0.2…v0.7).

## Appendix B — worked example (the P1 acceptance narrative)

1. An agent edits `ava_builtins/skills/ava-workflow/...` in a worktree; PR merges to main at 14:03.
2. 14:15 (next tick), each machine with the core channel ON runs `ava packages refresh`: `ls-remote` shows a new head; fetch (objects only); `git diff --name-only <applied_rev> <head> -- ava_builtins/skills/ava-workflow` → hits; archive-extract to staging; scan passes (builtin content); local-edit guard passes.
3. Staged swap lands the new `$AVA_HOME/skills/ava-workflow/`; registry records `applied_rev = <merge sha>`, `last_result = applied`.
4. No process was restarted; the next agent that opens the skill reads the new text; the capabilities index of live agents names the change on its next rebuild (existing before_llm hook).
5. `ava packages status` shows: `ava-workflow  core  auto  rev abc1234  applied 14:15`.
