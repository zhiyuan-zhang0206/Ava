---
type: doc
title: Prepared operator entry validation boundary
description: Retained CLI validation before an intentionally unimplemented dispatch.
---

# Prepared operator entry (validation only)

The explicit `ava cluster update --local --prepared PLAN` parser runs before
detached logging, mutable checkout anchoring, Settings, and ordinary commands.
Argparse abbreviations select the same early path. Invalid
flag combinations never fall through to the source updater or old gateway RPC.

The entry accepts only a canonical mode-0600 request below the installed unit's
`run/` directory and only from a retained POSIX wheel runtime. It binds the
loaded candidate image, complete declared roster and normal plan, original deadline,
schema baseline, and a distinct recovery image. Full receipt bytes are selected
and hashed by `prepared_receipt_digest`; the embedded `ExpectedUnitWriters`
tuple must independently reproduce the narrower `inventory_digest`.

After successful local validation the command returns exit code 2 with an
explicit refusal: authenticated dispatch and exact pre-stop abort are not
implemented, and no operation was started. It does not connect to Postgres,
write publication or deployment state, freeze admission, stop services,
migrate, change selectors, start services, or publish a release.

A direct runner-to-`deployment_state` acknowledgement is not an available
implementation: enrolled runners use the least-privilege `ava_runner` database
projection, which cannot update that singleton. A future implementation needs
an authenticated gateway/coordinator adoption boundary plus an exact abort that
can remove a no-effect pre-stop operation. Until both exist, local validation
must not be represented as an all-unit barrier or durable pending publication.
