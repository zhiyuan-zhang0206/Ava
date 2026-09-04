# Native launcher observation

The restricted bootstrap observer now reads native launcher declarations without
registering, unloading, reloading, or otherwise mutating an OS job. A launchd
declaration is identified by its label and the SHA256 digest of the raw plist;
a cron declaration is identified by the SHA256 digest of its exact line. The
observer repeats bounded reads so a declaration or loaded-state change during
collection degrades to unknown rather than becoming closure evidence.

Disk declaration identity remains separate from effective scheduler state. A
matching home and prepared executable path prove only what the declaration says.
An exact successful launchctl lookup proves loaded presence, while a failed
lookup, diagnostic text, scheduler liveness, effective enablement overrides, and
the loaded executable remain unknown. Unsupported, unreadable, expired, or
inconsistent evidence likewise remains unknown.

This slice deliberately adds no job registration, capability activation,
updater adoption, or fleet-closure decision. Windows Task Scheduler observation
and complete launcher inventory remain future work.
