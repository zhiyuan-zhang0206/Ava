---
type: doc
title: Inbound Provenance Facts
description: "Nullable server-owned credential, transport, content-hash, and source-assertion facts stored beside gateway-created inbound messages."
tags:
- shared
- identity
- audit
---

# Inbound provenance facts

`shared/inbound_provenance.py` defines the audit facts that a gateway boundary
passes to the durable inbound insert:

- `source_verified_by` names the credential kind and stable subject, never the
  credential secret.
- `source_transport` names the ingress path established by the server.
- `content_hash` is the lowercase SHA-256 of the exact persisted text.
- `source_assertion_match` compares `source=agent:N` with
  `source_verified_by=agent_token:M`. It is true or false only when both sides
  have that exact form; all unknown cases remain `NULL`.

The application rejects credential identities longer than 120 characters and
transport names longer than 80 characters before persistence; SHA-256 output
is asserted to be exactly 64 hexadecimal characters. The matching database
columns are `VARCHAR(120)`, `VARCHAR(80)`, and `VARCHAR(64)`, respectively, so
writers that bypass the application cannot store an oversized audit fact.

These columns are evidence, not authorization input. A mismatch is persisted
and delivery continues. Credential changes on a same-body idempotent retry do
not conflict with the first durable receipt; the original row retains the facts
observed when it was inserted. Writers that do not cross a gateway credential
boundary, including existing agent-side writers, omit the provenance object and
leave all four columns `NULL` as explicitly untraceable legacy data.

Parent: [[shared.ava.okf.md]].
