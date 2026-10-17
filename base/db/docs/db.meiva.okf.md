---
type: doc
title: Database access
description: Shared database connections, transaction access and code-version admission.
tags: [base]
---

# Database access

`base/db/` owns shared database connections, transaction access and code-version admission.
Its component nodes describe the current contracts and implementation.

## Documented components

- [[base/db/docs/code-version-gate.meiva.okf.md]] — Code version gate.

## Direct (admin-plane) dials

The URL a `direct=True` dial uses is derived from the home's cluster record, which sits above
this layer. A composition root therefore hands its `Database` a resolver from
`base/cluster/dataplane/direct_url.py`:

```python
resolver = ClusterDirectUrl(machine.host)
database = Database(config, gate=gate, direct_url=resolver)
```

The resolver returns a `DirectResolution`: the URL to dial together with `is_direct` and, when
it could not resolve, a `reason` naming the URL's host:port and the record's port.

A dial that needs a real backend asks for one, and is refused rather than silently pooled:
`connect(direct=True)` and `Database.direct_url()` raise `DirectUnavailableError`, worded once in
`connections.require_direct`. The callers that must have one are the session advisory lock, a
`pg_dump` (one snapshot across many statements), `VACUUM`/`REINDEX`, and provisioning DDL. An
ordinary connection never asks the resolver at all, so an agent-runner that only reaches the
pooler keeps dialing its access URL. A caller that renders the cluster's address instead of
dialing it — the Grafana datasource host:port — takes `Database.direct_resolution().url` as the
best answer available. A host that cannot classify a URL refuses exactly when the work truly
needs the real backend; a read-only verification (the checkpoint schema check) dials the
ordinary access URL.

The resolver owns one dependency, the lazy local-host reader; `DbConfig` remains the database
connection slice. Resolution reads that reader only when pooling is on and the URL's host is not
loopback, to tell this box's URL from a foreign one. Placeholder, unparseable, portless (a Unix
socket) URLs and every URL while pooling is off are direct without reading it, and no
resolution reads the cluster record for those shapes. A handle built without a resolver refuses
a direct dial.

The inbound-message writes, the wake publish and the live-agent reads that once lived in this
package are owned by [[base/agents/inbox/docs/inbox.meiva.okf.md]].

Test admission and dial-fixture evidence: [[test-database-safety.meiva.okf.md]].
