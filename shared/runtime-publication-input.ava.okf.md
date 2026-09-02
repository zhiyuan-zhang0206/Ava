# Runtime publication inputs

`runtime_publication_input.py` derives actual unit facts from the imported,
resolved runtime prefix, installed machine file and canonical selector. It never
selects the latest receipt, reads a request identity, or copies database expected
values. The existing selector's version-two shape is exactly version,
artifact_digest, manifest_digest and inventory_receipt_digest. The old exact
two-field shape provides no new-publication input; malformed versions refuse.

The selected full receipt lives at the existing content-addressed unit run path.
Its bytes, complete service roster and expected image/home are validated. The
loaded package's baseline file supplies an independent integrity comparison for
the verified image, not proof of live applied migrations. Selector and machine
facts are reread to reject drift. A source/dev runtime returns no new input.

Full image verification runs once per actual Python process or hosted-daemon
boot, outside the database lock. Ordinary claims and same-incarnation hosted
turns must not call it. Before a new hosted incarnation, the process-local
result has a cheap selector/manifest/full-receipt/machine binding check followed
by the same-transaction publication gate. Cross-process reuse or a changed
selector refuses; no shared global cache or environment flag bypasses validation.

This read-only output is not normal-service readiness, an all-writer closure, a
birth permit or protocol-one authority. Actual admission retains the separate
publication transaction gate. The selector writer and rollback CAS remain the
existing release owner's responsibility; this module performs no activation.
