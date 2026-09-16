# macOS start ownership and watchdog registration

The process that performs startup must own the lifecycle lock. A GUI handover
therefore leaves the caller's lock and maintenance authorization before it
launches or observes the GUI job. Removing lifecycle serialization entirely was
rejected: ordinary start/stop still mutate the same local resources. Having the
observer resume the cluster was also rejected: readiness and resume belong to
the child that established the serving generation.

Watchdog registration preserves a loaded job when its desired plist is
unchanged. Changed jobs wait for launchd to report removal before bootstrap;
bootout completion alone does not establish that its label can be reused.
The old plist remains until removal is confirmed so a failed unload cannot
make the next converge mistake the old loaded job for the desired one. Unknown
probe errors fail closed, and a probe never unloads its own scheduler ancestor.
Blind retries of arbitrary bootstrap errors were rejected because they obscure
invalid configuration and permission failures.

Regression coverage uses a real file lock for the GUI handover and a scheduler
model that delays removal after successful bootout. No production jobs are
modified by these tests. This change does not address checkpoint cold-read
performance or the Phase B deadline for unreachable runner ops endpoints.
