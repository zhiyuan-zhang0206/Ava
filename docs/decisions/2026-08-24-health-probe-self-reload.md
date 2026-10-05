# Health-probe self-reload guard

## Context

The macOS health probe can initiate automatic rollback, whose recovery path runs
`ava start` beneath the same LaunchAgent. Health-probe registration previously
reloaded the label unconditionally. `launchctl bootout` therefore terminated the
recovery process tree before rollback cleanup could resume the host and release
the update lease.

## Decision

Registration treats an inherited `XPC_SERVICE_NAME` equal to one of the current
home's health-probe labels — the path-only label or a transitional legacy label
— as an ownership boundary. It neither rewrites the plist nor reloads the job in
that context. Leaving the old plist intact is deliberate: the next external
converge can still detect a desired-content change or relabel and apply it.

The check is owned-label rather than “running under launchd.” An `ava start`
invoked by the separate boot-autostart job must still converge the health probe.

Writing the new plist while merely skipping reload was rejected because the next
converge would mistake the on-disk spec for the loaded spec and permanently lose
the deferred change. A detached reload helper was also rejected: it remains a
descendant of the job being unloaded and does not create a reliable ownership
boundary.
