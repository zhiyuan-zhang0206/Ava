---
name: ava-self-development
description: Changes Ava kernel code through isolated worktrees, reviewed PRs, CI, and an explicitly authorized verified deployment. Read before changing kernel or release behavior.
---

# Ava self-development

## Development is not deployment

Kernel changes belong in a development clone and isolated worktree, never in
the production checkout, virtualenv, or active plugin image. Do not create a
development worktree from production. Do not edit, reset, switch branches,
reinstall packages, or reload modules in a running production tree.

The running interpreter keeps imported modules. A merged commit, an on-disk
SHA, or a successful CLI exit does not prove running services adopted it.
Read `conventions/defensive-patterns.md`: a rollout cannot deliver its own
protection; the old orchestrator controls the first rollout of a new safeguard.

## Change workflow

1. Use a separate development clone and isolated worktree. Preserve existing
   uncommitted changes. Follow `ship-a-change` for PR and merge-queue mechanics.
2. Implement code, documentation and behavior tests together. Follow explicit
   user test-location constraints. When local tests or cluster boot are
   forbidden, use selected tests and required integration gates in CI.
3. Obtain review and QA against the exact PR head SHA. Prior-head approval
   does not approve later changes. Record real CI runs and negative controls;
   skipped checks or no workflow runs do not prove behavior.
4. Enqueue only after required review, CI and user/coordinator clearance.
   Merge proves repository integration, not production health.
5. Only the designated operator performs the separately authorized rollout.
   Contributors and QA agents must not launch competing deployments, bulk
   lifecycle operations, or production hotfixes.

## Deployment gates

Use the installed `ava cluster update --help` contract. Do not copy historical
drain timeouts or assume old code understands a new flag. Record before rollout:

- Exact machine, user, unit home and ports. Local process listings cannot
  establish remote machine state.
- Fixed target SHA; current installed/running versions; gateway, runner,
  schema and plugin compatibility.
- No conflicting rollout, live lease or unauthorized lifecycle actor.
- CI and exact-head QA/review evidence.
- Verified recovery point and supported rollback or fix-forward plan.
- Safe bootstrapping under the old imported orchestrator.

Prepare downloads, builds and remote backup transfer while serving when the
supported protocol permits. Never remove recovery gates to shorten maintenance.
Report actual phase elapsed time and failed-host state. Native data-plane
services remain native; Docker is not a deployment prerequisite.

## Verification and recovery

Use supported CLI lifecycle commands. Do not substitute raw signals, direct
database state changes, source resets, or hand-built per-host Git/package
installation sequences for deployment.

Verify every participating unit's target, installed and running versions,
service identities/start times, readiness, maintenance state, lease release,
and representative agent claim/exec progress. Disabled services and stopped
schedules must remain so. Hosted agents need an actual host consumer, not a
separate PID per agent.

Do not declare success from a filtered roster, pointer, CLI exit, or stale
health response. An offline host is an explicit incomplete result.

If rollout fails, preserve evidence and inspect the installed
`ava cluster recover --help` and `ava cluster rollback --help`. Confirm live
holder semantics and schema compatibility before acting. Do not blindly retry
updates or reset production source. If no supported safe path exists, report
the precise blocker and request a scoped recovery decision.

## Preview and CI

Preview clusters are isolated test infrastructure, never a reason to touch
production. Run them only where authorized. When local clusters are forbidden,
use CI-hosted native Postgres/Redis. Assert actual state/effects, not success
words in an agent's report. Distinguish setup failure, skipped execution and
test failure; preserve exact test/head identities and logs.

## SDK changelog

`reference/generate_sdk_changelog.py` compares SDK symbols and conventional
commits between refs. Run it in development. Changelog generation does not
deploy or establish runtime compatibility.
