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
this layer. A composition root therefore hands its `Database` a resolver:
`Database(config, gate=gate, direct_url=ClusterDirectUrl(machine.host))`
(`base/cluster/dataplane/direct_url.py`). A handle built without one refuses a direct dial.
The inbound-message writes, the wake publish and the live-agent reads that once lived in this
package are owned by [[base/agents/inbox/docs/inbox.ava.okf.md]].
