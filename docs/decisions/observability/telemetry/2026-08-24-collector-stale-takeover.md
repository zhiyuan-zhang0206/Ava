# Collector stale takeover

The OTel collector's :4318 reachability was insufficient evidence that the
current supervisor owned it: a native collector can survive its supervisor and
continue accepting empty OTLP requests, which left the watchdog green while
the replacement repeatedly failed to bind.

Startup and the watchdog now use one shared listener verdict over both the
OTLP receiver (:4318) and the collector health port (:8888). A holder is
reclaimed only when its resolved executable exactly matches this unit's
collector binary and it is absent from the live `ava-otel-collector` session
record. A different executable or a holder belonging to the live record remains
a terminal port conflict.

Port-only liveness and PPID-based reaping were rejected. The former cannot tell
an orphan from the expected collector; the latter would misclassify normal
detached native services. The recorded session identity plus resolved binary
provides the required ownership proof before a kill.
