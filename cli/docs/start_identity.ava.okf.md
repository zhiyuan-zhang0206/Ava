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
under the home and checkout binding locks (in that order) before publishing
`.env`. The intent is the home's record of itself: a gateway's ports and
data-plane host live in its `record` (`base/cluster/record.py`), and no host
file lists clusters. A new home records the fixed port table
(`base/host/env/port_table.py`) after a bind probe finds every port free; a
record whose slots differ from the table is refused. The checkout lock serializes
different homes; stale first-start inputs cannot replace an existing binding.
Pointer publication and retirement share that lock. An interrupted claim
resumes the same ports and credentials: the intent carries
the `.env` payload only while claiming and drops it once `.env` is published, so
no stale copy of a credential outlives a rotation there. A gateway claim
also pins a minted logical-backup passphrase before it publishes `.env`
(`services/gateway_side/backup/passphrase.ensure_minted`; an interrupted birth
keeps the first one). Conflicting identity,
unregistered existing resources, or a terminal destroy intent refuses startup.

`ava start --worktree` creates a gateway and runner with an isolated home and port
block. Explicit capabilities initialize other layouts. A remote runner uses the
same entry with `--gateway-url` and `--db-capability` (a sealed bundle minted by
`ava cluster db-authority issue-unit`, opened with its transport key from
`AVA_DB_CAPABILITY_KEY`); the bundle's machine API token authenticates the join
and, once verified, installs the unit's runner DB login and API token — a remote
runner never holds the human cluster secret (`AVA_CLUSTER_SECRET`; its presence
in a remote unit's `.env` refuses startup). First-start configuration
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
runner grants, then PgBouncer. `cli/commands/lifecycle/start.py` converges host prerequisites,
launches the selected root tree and records serving only after complete readiness.
Bare repeated start retains the desired service selection in `service-selection.json`.
A live root is admitted before preparation: changed source, environment, or roster
requires prior stop; identical live generations skip configuration, dependency,
and schema writes. Schema drift fails read-only. This development source digest
excludes ignored build outputs.

`cli/start_runtime.py` (`StartRuntime`) names what one start executes: the checkout
that loaded the code, its working directory and the interpreter that loaded it.
It is captured once, rechecked before each lifecycle phase, and grants no
migration, writer-closure or other operation right. Restart captures the same
runtime before recording lifecycle progress or stopping services and passes it
into startup. The exact maintenance hold remains closed until complete readiness
and authorized resume. Source convergence preserves host setup and scaffold
steps without automatic production editable-install repair or reinstall.

On macOS only, root must descend from the signed permissions helper. Linux root
starts directly or under systemd. Ordinary application services have no named-session
launcher or separate watchdog spawn owner. Terminal sessions and native data-plane
custody remain distinct resources.

The start intent records `claiming`, `configured`, `provisioned`, then `ready`.
This phase journal preserves initialization authority; it is not evidence of current
process health. Current readiness is freshly observed from the owned generation.

Destroy retains the home lock while closing native custody, then retires only
the worktree `.ava_home` pointer bound by the durable start intent before marking
the home detached. A changed or symlink pointer is preserved and refuses release;
a missing pointer permits an interrupted retirement to finish. Ordinary stop
retains that binding, credentials and the record.

Parent: [[cli.ava.okf.md]].
