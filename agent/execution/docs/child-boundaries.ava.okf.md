---
type: doc
title: Execution child boundaries
description: Current ownership boundaries and their executable evidence.
---

# Execution child boundaries

The disposable child communicates through the typed request/result envelopes and the parent's output pipe. It owns catchable SIGINT/SIGTERM handlers, translating them to KeyboardInterrupt/TimeoutError so user cleanup can run; the parent owns escalation when native code or rewritten handlers cannot cooperate. The legacy watchdog is a daemon timer and exits 124 after the parent's timeout/grace margin: a pending timer must not keep an otherwise completed interpreter alive. This is separate from the gated-domain deadline owner.

The executable entry prepares logging before loading SDK/configuration. Embedded callers retain their sinks. Line-buffered stdout/stderr carry user output; framework logging stays on its file sink. `ChildContext.boot_started_at` measures setup after initial imports through the ready boundary, whereas parent elapsed time also includes user execution.

Overlay carriers are popped once, with birth values applied before overlay values. Framework fields apply before settings reads, plugin fields after runtime faces register their fields. A stateful request initially retains raw state: first access loads faces, builds the dynamic state schema, then decodes/validates. A stateless or untouched request does not pay serde costs. Empty state-update dictionaries carry no delta; tampering with the update slot is a reported error.

Agent source is registered for traceback line lookup. The child copies its user-code builtins binding instead of mutating the process-global module. Each execution binds one SDK tally shared by ordinary threads; boot calls precede that binding. The code-reached flag distinguishes a boot failure from code that ran without output. User-facing traceback text contains agent frames; the envelope retains the complete error. Lifecycle outcomes already have their inbound command and are not crashes. SIGINT/SIGTERM exception kinds alone cannot certify parent-requested cancellation: the parent retains that decision.

The result envelope is written before last-mile telemetry. Known unfinished ordinary delivery is best effort; an unknown owned writer failure rewrites a successful result, while an existing crash remains primary. Timeout/cancel paths skip delivery. A zero-record finalizer leaves telemetry and OTel unimported. Deferred OTLP initialization occurs while the interpreter is alive, not during finalization. The stdlib crash writer remains usable if application logging/protocol imports fail; writing the fallback envelope precedes best-effort logging.

```ava-contract
id = "exec-child-result-boundary"
kind = "invariant"
statement = "The child preserves the code-reached distinction and a full crash envelope, including a fallback when the normal protocol writer fails."
code = ["agent/execution/child.py:<module>"]
tests = ["agent/tests/execution/test_exec_child.py::test_boot_config_failure_writes_crashed_envelope", "agent/tests/execution/test_exec_child.py::test_crash_envelope_uses_stdlib_fallback_when_protocol_writer_fails", "agent/tests/execution/test_exec_child.py::test_child_crash_writes_envelope_with_traceback"]
```

Parent: [[agent/execution/docs/gated-deadline.ava.okf.md]].
