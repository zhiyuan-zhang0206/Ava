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
under the home and registry locks before publishing the registry record or `.env`.
An interrupted claim resumes the same ports and credentials. Conflicting identity,
unregistered existing resources, or a terminal destroy intent refuses startup.

`ava start --worktree` creates a gateway and runner with an isolated home and port
block. Explicit capabilities initialize other layouts. A remote runner uses the
same entry with `--gateway-url` and a bearer supplied through the environment;
the gateway projection must identify the runner DB role. First-start configuration
can come from `--config-file`; home, credentials and derived resource identity
cannot be overridden by generic configuration. Reusing a different configuration
file for the same recorded initialization refuses.

The native ordering is storage, owned database and checkpoints, migrations and
runner grants, then PgBouncer. `cli/commands/start.py` converges host prerequisites,
launches the selected root tree and records serving only after complete readiness.
Bare repeated start retains the desired service selection in `service-selection.json`.
A live root is admitted before preparation: changed source, environment, or roster
requires prior stop; identical live generations skip configuration, dependency,
and schema writes. Schema drift fails read-only. This development source digest
excludes ignored build outputs and does not certify an immutable release image.

On macOS only, root must descend from the signed permissions helper. Linux root
starts directly or under systemd. Ordinary application services have no named-session
launcher or separate watchdog spawn owner. Terminal sessions and native data-plane
custody remain distinct resources.

The start intent records `claiming`, `configured`, `provisioned`, then `ready`.
This phase journal preserves initialization authority; it is not evidence of current
process health. Current readiness is freshly observed from the owned generation.

Parent: [[cli.ava.okf.md]].
