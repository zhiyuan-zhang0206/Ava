---
type: doc
title: Idempotent cluster start
description: One settings-free identity phase followed by native provisioning and root-owned readiness.
tags:
- cli
- cluster-lifecycle
---

# Idempotent cluster start

`cli/start_intent.py` resolves and validates first-start inputs without importing
runtime Settings. `cli/start_identity.py` persists the complete private intent
under the home, checkout binding, and registry locks (in that order) before
publishing the registry record or `.env`. The checkout lock serializes different
homes even when they use separate registries; stale first-start inputs cannot
replace an existing binding. Pointer publication and retirement share that lock.
Registry paths cannot alias lifecycle data or locks, and registry lock waits are
bounded so contention cannot indefinitely consume a lifecycle operation.
An interrupted claim resumes the same ports and credentials. Conflicting identity,
unregistered existing resources, or a terminal destroy intent refuses startup.

`ava start --worktree` creates a gateway and runner with an isolated home and port
block. Explicit capabilities initialize other layouts. A remote runner uses the
same entry with `--gateway-url` and a bearer supplied through the environment;
the gateway projection must identify the runner DB role. First-start configuration
can come from `--config-file`; home, credentials and derived resource identity
cannot be overridden by generic configuration. Reusing a different configuration
file for the same recorded initialization refuses.

First start admits the host's ordered tool directories into `AVA_SERVICE_PATH`
in the existing intent and `.env`, excluding the caller's virtualenv. Retries
reuse that declaration even when the caller PATH changes. Managed root children
prepend the current runtime virtualenv, then the admitted host directories and
provisioned tool defaults. The declared value wins over an inherited copy;
interactive terminals retain their separate environment policy. PATH remains
part of the live generation digest, so editing the declaration requires stop
before another start. An existing home without this declaration requires an
explicit cutover edit; repeat start never silently recaptures its caller PATH.
Admitted directories must be absolute and survive literal `.env` round-trip;
comment-sensitive names and interpolation expressions fail before intent creation.

The native ordering is storage, owned database and checkpoints, migrations and
runner grants, then PgBouncer. `cli/commands/start.py` converges host prerequisites,
launches the selected root tree and records serving only after complete readiness.
Bare repeated start retains the desired service selection in `service-selection.json`.
A live root is admitted before preparation: changed source, environment, or roster
requires prior stop; identical live generations skip configuration, dependency,
and schema writes. Schema drift fails read-only. This development source digest
excludes ignored build outputs and does not certify an immutable release image.

`cli/start_runtime.py` admits a captured `VerifiedRelease` for an initialized home.
It verifies the complete inventory, build commit, actual loaded interpreter and
module paths, and isolated `-I -B` execution. Release start must match the home's
selected artifact and manifest; a source checkout cannot start a home with a
release selector. The external release operation supplies these captured facts
and owns selection, migration permission, recovery and known-good publication.
Image verification by itself grants none of those decisions.
Ordinary start accepts this runtime identity directly. Old candidate receipts,
updater telemetry flags and parent-process rollout markers grant no startup
rights. The durable home operation gate runs before preparation, and the exact
maintenance hold remains closed until complete readiness and authorized resume.
Source convergence preserves host setup and scaffold steps without automatic
production editable-install repair or reinstall.
Restart captures the same runtime before recording lifecycle progress or stopping
services. Installed restart requires that the currently loaded isolated image
matches the selected verified inventory; source restart refuses a retained-image
home. Both check the durable home operation gate before effects, recheck before
stop, and pass the identical captured runtime into startup.

The retained operation also binds the authoritative configuration through the
Settings-free `shared/start_inputs.py` digest. Start, preflight, observation and
resume refuse changed inputs; stage checks precede Settings loading and repeat
after startup or readiness. The explicit startup capability rechecks this digest
before ordinary release start prepares identity or loads Settings. These checks
detect drift at lifecycle boundaries; they do not lock arbitrary file writers.

The same start and root driver consume this runtime identity. Release manifests
contain direct captured executable/module argv, and the root uses the verified
working directory. Their generation binds artifact/manifest identity, environment,
and home configuration, including desired service selection, without invoking Git.
Configuration inputs must be regular files; a dangling or replaced symlink cannot
silently become a default configuration. Repeated identity preparation writes
no checkout pointer or lock inside the sealed image. Verified assets do not run
source converge, npm installation, extension materialization, or implicit SQL
migrations; storage ownership, pooler and consumer readiness still run. The
current release path requires an already provisioned home and current schema.
Fresh release initialization and schema-changing transitions require separate
preparation and migration authority; no checkout fallback supplies them.

On macOS only, root must descend from the signed permissions helper. Linux root
starts directly or under systemd. Ordinary application services have no named-session
launcher or separate watchdog spawn owner. Terminal sessions and native data-plane
custody remain distinct resources.

The start intent records `claiming`, `configured`, `provisioned`, then `ready`.
This phase journal preserves initialization authority; it is not evidence of current
process health. Current readiness is freshly observed from the owned generation.

Destroy retains the home lock while closing native custody, then retires only
the worktree `.ava_home` pointer bound by the durable start intent before freeing
the registry slot. A changed or symlink pointer is preserved and refuses release;
a missing pointer permits an interrupted retirement to finish. Ordinary stop
retains that binding, credentials and the reservation.

Parent: [[cli.ava.okf.md]].
