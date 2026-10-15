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

- [[base/db/docs/code-version-gate.ava.okf.md]] — Code version gate.

## Direct (admin-plane) dials

The URL a `direct=True` dial uses is derived from the home's cluster record, which sits above
this layer. A composition root therefore hands its `Database` a resolver from
`base/cluster/dataplane/direct_url.py`:

```python
resolver = ClusterDirectUrl(machine.host, gateway_url_reader=lambda: boot.view.gateway.gateway_url)
database = Database(config, gate=gate, direct_url=resolver)
```

A handle built without one refuses a direct dial.
The resolver owns the required gateway URL reader; `DbConfig` remains the database connection
slice. Resolution reads that URL only when pooling is enabled and the database host is remote,
to distinguish the gateway's unresolved pooler from a direct SaaS database. Placeholder,
unparseable, portless and local URLs do not read it. Missing configuration propagates as an
error; an explicitly empty gateway URL names no gateway host.

The inbound-message writes, the wake publish and the live-agent reads that once lived in this
package are owned by [[base/agents/inbox/docs/inbox.ava.okf.md]].

Test admission and dial-fixture evidence: [[test-database-safety.ava.okf.md]].
