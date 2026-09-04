# Runtime publication input reconstruction

PR #1538 was reconstructed on top of the merged managed-writer publication and
restricted updater foundations rather than rebasing its historical stack. The
retained scope is the read-only resolver that derives publication input from the
loaded wheel generation, canonical selector, installed machine identity, and
the content-addressed preparation receipt. It does not add a selector writer,
normal-service readiness, admission callsite, or protocol activation.

The reconstruction preserves the publication foundation's two-digest boundary.
`inventory_digest` identifies the narrower `ExpectedUnitWriters` tuple consumed
by the observer. `prepared_receipt_digest` identifies the complete sealed
receipt, including service-only roster declarations. The version-two selector
names the latter explicitly, and the resolved `PublishedUnit` retains both so a
service-roster change cannot alias the observer tuple.

Full image verification remains a once-per-process boot operation outside the
database transaction. A process-local revalidation checks the pinned runtime,
selector bytes, manifest, receipt, and machine identity without rehashing the
whole image. Source runtimes and the exact legacy two-field selector grant no
publication input; malformed, moved, cross-process, or changed evidence refuses.
