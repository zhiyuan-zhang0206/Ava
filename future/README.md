# future/ — TODOs & design drafts

> This directory holds unimplemented design drafts, improvement plans, and recon reports.
> Completed items are deleted (OKF is the source of truth for current state).
> A completed item that carried a load-bearing design decision moves to
> [`decisions/`](../decisions/) instead of being deleted.

Organized by **direction**:

- **coding/** — coding agent ceiling, benchmark performance, coding-related follow-ups
- **infra/** — infrastructure (data plane / config / ops / tests / toolchain)
- **roadmap/** — the strategic capability roadmap

Every row below states what is **left**, not what the doc is about — a doc with
nothing left does not belong here.

## coding — moved beside their modules (agent/ + ava_builtins/)

These four plans co-locate with the code they plan for, per the 2026-08-12 doc ruling; the rows keep tracking what is left.

| File | What's left |
|------|------|
| [Ava / Ava Code prompt architecture](../agent/prompt-architecture.md) | Living doc; the core-vs-`ava_code` responsibility split + the mechanics-vs-behavior axis. Open: malicious-code refusal stance (pending a threat-model call), a minor whitespace nudge. The `ava_code` memory layer was **rejected**, not deferred |
| [Default bundled skills](../ava_builtins/default-skills.md) | Which external capability packs ship as repo defaults. Direction settled (**vendor-and-adapt**); open: per-overlap reconciliation, vendor location |
| [Compaction redesign](../agent/compaction-redesign.md) | Forced / command / spontaneous compact mechanics |
| [Import an existing agent's history](../agent/import-existing-agent-history.md) | Onboarding demo script — distil a new user's Claude Code / Codex history into the memory pool. Gated on going public |

## infra/

| File | What's left |
|------|------|
| [Agent-runner as server](infra/agent-runner-as-server.md) | Implemented as the sole runtime; remaining question is host fault isolation |
| [Vendored data-plane binaries](infra/vendored-data-plane-binaries.md) | **Redis leg only** — Postgres vendoring landed (`shared/runtime_binaries.py`), redis still comes from brew/apt. Also the single home for slice 3 of the doc below |
| [Embedded per-cluster data plane](infra/embedded-per-cluster-data-plane.md) | Design record; slices 1+2 done. Only the redis half of slice 3 remains, tracked in the row above |
| [Auth / TLS design](infra/auth-tls-design.md) | **Phase 3 (TLS) only** — Phase 1 (fail-closed gateway auth) and Phase 2 (cookie session auth) are deployed |
| [Cluster consistency: commit-level pinning](infra/commit-pinned-cluster.md) | Increments A + B built and drift now self-heals via `ops/controllers/pin.py`. The remaining hard fail-fast enforcement is **overtaken** by the fail-fast-vs-reconcile decision and needs re-litigating before it is built |
| [Ops module](../ops/ops-module.md) | Spec / Status / controllers / manager built. Left: `ops/identity.py` and the shared **Drain** primitive (still `cli/commands/update.py`); pg-backup is a supervised scheduler service |
| [Extension ownership](infra/extension-ownership.md) | **Everything — design for issue #39, slices S1–S5.** Cluster-owned content + enablement (PG rows + blobs), machine demoted to a computed capability set, per-agent activation overlay; partially supersedes the two rows below when it lands |
| [Decentralized install + local config](infra/decentralized-install-and-config.md) | **Hooks-only plugin bundles only** — everything else (install registry, CC plugin materialization, `type="mcp"` packages, the cross-machine inventory UI) landed. The per-machine enable-state direction is proposed to be reversed by [extension-ownership](infra/extension-ownership.md) |
| [MCP scope & bundling](infra/mcp-scope-and-bundling.md) | **Formalizing per-machine scope** — all three config sources landed; there is still no `scope` field, and the one real machine-scope consumer (the shared browser MCP upstream) is hand-built. [extension-ownership](infra/extension-ownership.md) proposes dissolving the scope field into capability matching |
| [Prompt injection — boundary map](infra/prompt-injection.md) | The content-layer scanner (`ava/security.py`) is built and default-on. Left: two coverage gaps (content-source skills, `ava.understand(url)`) and the whole structural boundary (sandboxed reader, egress allowlist) |
| [Skill supply-chain trust](infra/skill-supply-chain-trust.md) | The install gate + trust tier are built. Left: recall must enforce the tier (highest value), a re-scan sweep when the rule table grows, a human-presence channel for `--accept-risk`, publisher signatures |
| [PG backup — off-site leg](infra/pg-backup.md) | Off-site leg (GCS / R2) for the disk-loss scenario; the local daily `pg_dump` landed |
| [Release-directory atomic code swap](infra/release-dir-atomic-code-swap.md) | Deferred, not started — document the immutable-artifact swap mechanism |
| [Release cadence: self-scheduling by Ava](infra/release-self-scheduling.md) | Once bootstrapping is done, Ava schedules its own releases |
| [Living debt tracker](infra/debt-tracker.md) | Skeleton — a single "what debt is open now" view maintained by the sweeper engine |
| [DB write batching](infra/db-write-batching.md) | Design draft |
| [Heartbeat design](infra/heartbeat-design.md) | Research record; Tier 2 shipped as a simpler opt-out design. Kept for the rejected two-tier proposal |
| [Model providers as plugins](../shared/lm/model-providers-as-plugins.md) | **Mechanics + Grok pilot landed** — registry, dispatch, vocabularies, key channel, and lazy load are built. Left: plugin dependency installation and deciding which remaining core providers should extract |

## Top level

| File | Role |
|------|------|
| [**Roadmap**](roadmap/README.md) | ★ The live, ordered list of what Ava builds next. Buckets: **now**, **north star**, gated-on-sandbox, open-source prerequisites, low-priority, deliberate-no list |
| [Distribution form: one core, packaging is a gated outer layer](distribution-form.md) | Distribution architecture |
| [Frontend plugin contributions](frontend-plugin-contributions.md) | **All of it** (design settled, issue #57): `contributions.ui` manifest key + validator, theme token packs, plugin page mount + nav, agent-inspect sections, blessing the gateway API for alternative frontends — slices U1–U5 |
| [web-ai — drive frontier-model web apps](web-ai.md) | Driving ChatGPT / Gemini / Claude through a logged-in browser |

> **Cleaned up 2026-07-28.** Deleted as fully landed with no load-bearing decision
> left to keep: `preview-validation-suite`, `preview-cluster`,
> `web-fetch-skill-routing`, `config-decomposition`, `plugin-extension-api`,
> `dynamic-workflow`, `fleet-view-design`, `ava-code-memory` (the last one
> *rejected* — memory belongs to the cluster's memory plugin, and repo facts belong
> in that repo's `AGENTS.md`). Moved to [`decisions/`](../decisions/) because
> the decision outlived the plan: `multihost-deployment` →
> `2026-06-11-multihost-deployment`, `state-surface-canonicalization` →
> `2026-05-22-state-surface-canonicalization`, `cron-to-scheduler-cutover` →
> `2026-07-01-cron-to-scheduler-cutover`. `self-evolution-rollback` was deleted
> rather than moved — `2026-06-29-self-evolution-rollback` already covered it.
>
> An earlier pass (2026-07-25, Task #455) deleted di-refactor, compact-summary-guard,
> test-env-followups, agent-label-improvements, update-failure-recovery,
> vps-attack-surface, agent-events-stats-index, and supervision-queue, and dropped
> the release-history dir (release history lives in the annotated git tags + `CHANGELOG.md`).
