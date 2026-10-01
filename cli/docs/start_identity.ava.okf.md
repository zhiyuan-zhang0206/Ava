---
type: doc
title: Home identity, init and start
description: '`ava init` records a home''s identity once, settings-free and starting nothing; `ava start` admits that home, then runs native provisioning and root-owned readiness.'
tags:
- cli
- cluster-lifecycle
---

# Home identity, init and start

`cli/init_intent.py` (`ava init`) resolves and validates the first-start inputs without
importing runtime Settings: the home is `AVA_HOME` (else `~/.ava`), and a home that
carries its own `<home>/source` checkout is initialized and started only from that
checkout. `cli/start_identity.py` persists the complete private intent under the
home's start-intent lock before publishing `.env`, and init ends there: it starts no
process and creates no database, so a home that stops at `configured` holds nothing
native. `cli/start_intent.py` (`ava start`) admits only such a home
(`require_initialized`: the intent is past `claiming`, `.env` carries the identity the
roles require, `AVA_SERVICE_PATH` is declared, a remote unit holds no human secret) and
refuses everything else naming `ava init`, a home with no intent included. The intent is the home's record of itself: a gateway's ports and
data-plane host live in its `record` (`base/cluster/record.py`), and no host
file lists clusters. A new home records the fixed port table
(`base/host/env/port_table.py`) after a bind probe finds every port free; a
record whose slots differ from the table is refused. The intent's `checkout` is the
home's admitted runtime: the database-authority delivery admits only a process running
from it (`base/cluster/authority/delivery.py`). An intent that still carries the
`worktree` key an older start recorded is refused by name rather than read. An
interrupted claim
resumes the same ports and credentials with `ava init` and no flags (a flag is
refused): the intent carries
the `.env` payload only while claiming and drops it once `.env` is published, so
no stale copy of a credential outlives a rotation there. An initialized home
(`configured`, `provisioned` or `ready`) refuses a second `ava init` and changes
nothing; its identity changes only through `ava cluster destroy` and a new init. A gateway claim
also pins a minted logical-backup passphrase before it publishes `.env`
(`services/gateway_side/backup/passphrase.ensure_minted`; an interrupted birth
keeps the first one). Unregistered existing resources, a terminal destroy intent, or a capability set
that differs from the intent's refuses a start.

`ava init` needs explicit capabilities (`--serve-gateway`, `--serve-agent-runner`,
`--serve-observability-station`) and a machine name. A remote runner gives it
`--gateway-url` and `--db-capability` (a sealed bundle minted by
`ava cluster db-authority issue-unit`, opened with its transport key from
`AVA_DB_CAPABILITY_KEY`); the bundle's machine API token authenticates the join
and, once verified, installs the unit's runner DB login and API token. That join
(`cli/unit_join.py`) is also what `ava cluster db-authority install-unit` runs for a
later bundle on an initialized unit; `ava start` makes no join, since a started
runner fetches its configuration through Settings and probes its gateway itself. A
remote runner never holds the human cluster secret (`AVA_CLUSTER_SECRET`; its
presence in a remote unit's `.env` refuses startup). Initial configuration
can come from `--config-file`, for a home with no `.env` yet; home, credentials and
derived resource identity cannot be overridden by generic configuration.

`ava init` admits the host's ordered tool directories into `AVA_SERVICE_PATH`
in the intent and `.env`, excluding the caller's virtualenv. A resumed init
reuses that declaration even when the caller PATH changes. Managed root children
prepend the current runtime virtualenv, then the admitted host directories and
provisioned tool defaults. The declared value wins over an inherited copy;
interactive terminals retain their separate environment policy. PATH remains
part of the live generation digest, so editing the declaration requires stop
before another start. An existing home without this declaration requires an
explicit cutover edit: `ava start` refuses it and never silently recaptures its caller PATH.
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

The start intent records `claiming`, `configured`, `provisioned`, then `ready`:
`ava init` takes a home to `configured`, the first start's data-plane birth to
`provisioned`, and a start that reaches full readiness to `ready`. A start accepts any
of the last three and a `configured` home is born by it (the birth branches of
`cli/commands/data_plane/bringup.py` follow the phase, not the command). This phase
journal preserves initialization authority; it is not evidence of current
process health. Current readiness is freshly observed from the owned generation.

Destroy retains the home lock while closing native custody, then retires the home's
OS jobs before marking the home detached. Ordinary stop retains the credentials and
the record.

Parent: [[cli.ava.okf.md]].
