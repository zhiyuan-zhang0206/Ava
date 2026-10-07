# SDK reminder reinforcement

The SDK reminder originally optimized only for low repetition: native-Python
categories appeared at most once per context window. That default remains the
right general posture, but some agents repeatedly fall back to the same native
idioms even after seeing the hint. The four code categories therefore gained
one shared opt-in `every_time` cadence while keeping `once_per_compaction` as
the default. A single knob keeps their reinforcement policy coherent; the
watcher-aware silent wait case remains silent under either cadence.

The disposable interpreter boundary also needed a targeted reminder. A broad
hint on every `NameError` was rejected because most such errors are ordinary
typos or missing definitions. The hint fires only when the undefined identifier
appeared as a whole name in an earlier `execute_code` cell, which is evidence
that the agent assumed variables persisted between calls. Keywords and builtins
are excluded, the behavior is default-on but configurable, and each name fires
at most once per context window to avoid amplifying retry loops.
