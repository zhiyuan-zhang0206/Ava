# Core package update channel: core skill/plugin updates get their own delivery cadence

## Context

Core content (builtin skills, plugin-carried skills, builtin plugins) is authored in this
repository and ships inside the code train. Today it moves only when the code moves: an
`ava cluster update` rollout, an explicit `ava skill update`, or nothing. Two pressures made
that insufficient:

- A skill edit merged to `main` could stay invisible in a running fleet for days (the
  2026-08-27 incident: two days between merge and runtime).
- Builtin plugins are checkout code, so a plugin edit anywhere requires the whole L4 train
  (PR → CI → merge → cluster update → restart) — conceptually L3 work forced through
  L4 machinery.

The 2026-09-11 request: core content MAY stay coupled to this repository (a core skill may
reference SDK internals directly), but it must update on a cadence of its own (e.g. daily);
installs choose a per-package update mode and check interval; and a high-frequency skill edit
must not disturb the running cluster.

Constraints the design must respect: the prod checkout invariant ("tree == installed commit",
reset + clean); rollouts remain the only code/schema/service channel (CLI-only, agent-trigger
forbidden); every ingest passes the supply-chain scan gate and never auto-promotes trust;
plugin activation stays at process boundaries; retention must be disk-bounded.

## Decision

Add a per-machine **content channel** that delivers core content independently of the cluster
update:

1. **Channels.** `core` = this repository's content paths, fetched from its remote by ref
   (objects-only fetch; the checkout tree is never touched); `git` = a package's recorded
   source (the existing flow). Local packages have no channel and are skipped.
2. **Policy plane.** Per package: `mode` ∈ {`auto`, `notify`, `off`} plus
   `check_interval_seconds`. Default for every channel-backed package: `auto` @ 24h; manual
   refresh is always available; the core channel tracks `main` (merge = publish). Overridable
   per package at install and later.
3. **Executor.** One verb — `ava packages refresh` — run manually, by a converge-registered
   per-machine OS job (15-minute base tick; intervals are data), and later by a host pass.
   Check (`ls-remote`) ≠ fetch: content is fetched only when the ref moved, extracted into a
   staging dir, gated (scan / manifest-version / local-edit guard), then stage-swapped
   atomically; the previous tree backs `ava packages rollback`.
4. **Activation at natural boundaries only** (user ruling 2026-09-13). Skills are hot on the
   next scan; plugins activate at the next process start. The refresh pass never restarts
   anything, never touches code/schema/services, and never writes the checkout.
5. **Version axis = commit-date-derived host version + a `requires_commit` ancestry layer**
   (user ruling 2026-09-13). No `pyproject` bump discipline; the dormant dated-release
   pipeline is not a dependency. `engines.ava` ranges remain for third-party content.
6. **Phasing** (user ruling 2026-09-13): P0 schema/status surface → P1 skills fast lane (the
   POC) → P2 core-plugin materialization → P3 cluster-row alignment with extension ownership
   (issue #39); P2 is scheduled after P1.
7. This **revises the delivery half** of ruling 2 in
   `2026-08-19-four-layer-modification-model.md`: builtin plugins stay authored in the kernel
   (the base set does not move out), but they are delivered and refreshed through the core
   channel — once P2 lands, a plugin edit no longer requires an L4 rollout.

Buildable detail: [`future/infra/core-package-update-channel.md`](../future/infra/core-package-update-channel.md).

## Alternatives rejected

- **Git submodule as the runtime mechanism** — fights the "tree == installed commit, reset +
  clean" invariant (a floating submodule reads as permanently dirty, or needs a whitelist),
  and every update leg would need a submodule story; silent-revert risk if any flow runs
  `git submodule update`. The pinning value is reproducible with a lock file.
- **Independent content repository now** — the right evolution shape when content needs its
  own release train or outside contributors, but it splits history, CI, and the "where do I
  send a fix" question today for no present benefit. The channel model keeps that move a
  source swap; it is an evolution path, not this work.
- **Ancestry comparison for the rollout refresh legs** ("apply checkout content only if it is
  newer by git ancestry") — clever, untestable at fleet scale; replaced by the explicit rule
  that rollout legs skip channel-managed packages.
- **A static `pyproject.toml` version (or hand-bumped CalVer) as the gate anchor** — either
  re-introduces hand maintenance or anchors on a number nothing in the deploy path reads; the
  commit date is already recorded and advances by itself.
- **In-process plugin hot reload / service restarts to activate content** — activation stays
  at process boundaries; the refresh pass is explicitly non-disruptive.
- **SKILL.md frontmatter as the manifest carrier** — a second parsing path with YAML quoting
  hazards; a sidecar manifest reuses the existing `ava-plugin.json` validator and range
  algebra.

## Consequences

- Core content gains a second delivery path; `ava cluster update` stays the only code channel.
  The rollout's builtin-skill refresh legs must skip channel-managed packages.
- Install registry schema v2 (`UpdateState` / `ChannelState`) plus `ava packages status`
  become the source of truth for "which rev is on disk, what is blocked, what is due".
- The refresh pass runs on pet machines: bounded budgets, per-home flock, skip while an update
  is in flight, one previous tree per package, no mirrors in P1.
- P2 materializes core plugins into the standard external root; module-identity surfaces
  (checkpoints, ledger, provider loading, ops discovery) get verified and locked, and the
  collision rule (managed shadowing) is implemented with a test.
- Trust and scan posture are unchanged: automatic paths never pass `--accept-risk` and never
  promote trust; blocked refreshes stay visible in status and unblock when the host moves.
- Package removal is not a refresh operation — a package whose source disappeared is left for
  the converge cleanup path.

Related: `2026-08-19-four-layer-modification-model.md` (amended by this entry),
`2026-08-05-cli-only-updates.md`, `2026-08-21-extension-ownership-three-tiers.md`,
`conventions/plugin-spec-v2.md`, issue #39, issue #42.
