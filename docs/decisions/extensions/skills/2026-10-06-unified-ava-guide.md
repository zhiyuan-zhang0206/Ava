# One Ava Guide for humans and internal/external agents

## Decision

Ava Guide is the shared entry point for understanding, deploying, operating,
and extending Ava. Organize it by task; each procedure states the execution
surface and identity it needs. Keep a compact root map and load detailed
sub-skills and resources on demand.

Move deployment, incident operations, modification layers, package installation,
native plugin development, schedule writing, and external-agent execution into
`ava_builtins/skills/platform/ava-guide/`. Their canonical paths are `deploy`,
`operations`, `modification-layers`, `packages/install`, `plugins/develop`,
`schedules`, and `external-agents` respectively. Keep existing `ops` for cluster
lifecycle and releases. The L1–L4 ownership and activation boundaries remain
those recorded in [the modification model](2026-08-19-four-layer-modification-model.md).

Move AI capability/timescale calibration into `ava-workflow/capability-timescale`.
Workflow owns decisions about scope, feasibility, delegation, and evaluation;
Guide owns the Ava-specific mechanisms for executing those decisions. Capability
judgments use current task-relevant evidence rather than a fixed improvement
multiplier. Keep `skill-creator` independent as reusable authoring methodology,
and keep kernel contribution and takeover-executor manuals project-local.

## Why

The previous guide primarily addressed an Ava agent operating itself through
CLI commands. Separate top-level deployment, extension, schedule, and external
worker skills required readers to cross package boundaries to complete one Ava
task. A shared task hierarchy serves all three reader groups while preserving
important distinctions between their execution identities and authority.

Avoid a single large instruction body: scripts, references, and detailed
procedures stay with their sub-skills. Avoid organizing parallel guides by
reader: common runtime facts have one owner, with interface differences stated
where the operation requires them.

## Distribution and migration

Gateway package and schedule draft prompts use the new nested skill paths.
The host-global external-agent bridge publishes the complete Guide with its own
per-client ledger. It preserves legacy external deployment/operator copies and
ledgers without refreshing or adopting them; user-modified trees retain the
existing conflict protection. This contribution does not roll out a deployment.
