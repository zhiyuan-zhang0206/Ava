# A broad handler re-raises or reports; a swallowed failure is a bug

## Context

The `ava_code` after_exec hook read per-turn state from the host process, raised
`PluginStateOutsideTurnError` on every call, and a `contextlib.suppress(Exception)` ate it. The
hook was a no-op from the day it shipped and wrote not one log line. Nothing about the failure was
distinguishable from a feature that works.

An AST sweep of production code (`agent`, `ava`, `ava_builtins`, `base`, `cli`, `gateway`, `ops`,
`services`, `scripts`) found 207 sites of the same shape: broad `except` / `suppress(Exception)`
whose body neither re-raises nor reports, or reports only at debug/info. They fell in three groups:
cleanup and probe code where the failure had a known, expected type; code that wrapped a call
"best-effort" with no real reason; and boundaries (daemon loops, telemetry seams) that must keep
going and so need to say what failed.

## Decision

A broad handler (`Exception`, `BaseException`, bare, or `suppress` of either) must do one of:
re-raise, log at WARNING or above (with the traceback), emit a telemetry event, write to stderr,
or hand the bound exception on (error result, failure list, future, `add_note`). A debug or info
line is not a report. In order of preference a violation is fixed by:

1. narrowing to the exception types the body can legitimately raise (expected conditions then
   stay quiet: `FileNotFoundError`, `ProcessLookupError`, `OSError` on a closing socket,
   `asyncio.CancelledError`);
2. deleting the handler so the failure surfaces;
3. keeping the broad handler at a real boundary and logging at WARNING with the traceback.

`scripts/lint/diagnostics/no_silent_failures.py` (hook `lint-no-silent-failures`) enforces it on every
production Python file; the repo is clean, there is no allowlist file. The only exemption is an
inline `# silent-ok: <reason>` on the handler line, reserved for a reporting channel's own failure
path, where reporting is impossible or would recurse.

The emitter's own best-effort seams (sink export, lazy pipeline init, exit-time closes, OTLP
mapping and flush) share one primitive, `base.telemetry.failure_isolated(sink)` /
`report_sink_failure`: the failure never reaches the producer or the drain thread, and is
reported through the `_no_emitter` diagnostic path (first occurrence and every 50th, so a seam
that fails on every batch is loud without flooding), falling back to stderr when logging itself
is down.

## Alternatives rejected

- **A baseline / allowlist of existing sites.** That is the state the sweep found: 207 sites that
  nobody had to justify. Every exemption is now visible at its site, with a reason.
- **Flag `except Exception` outright, whatever the body.** A handler that re-raises, translates, or
  returns the error to its caller is not silent; banning it would push code toward worse shapes.
- **Also flag narrower swallowers (`ValueError`, `RuntimeError`, `AttributeError`).** Most of the
  ~250 of them are parse fallbacks and capability probes where the type is the whole contract.
  They stay a manual audit target; broad types are the rule's scope.
- **Count any log call as a report.** A debug/info line is below every production sink's
  threshold; it is exactly how `logger.debug("... failed")` hid real failures.

## Consequences

- A newly broad handler fails the commit hook until it reports or narrows.
- Several formerly swallowed failures now propagate (fail-fast) or log at WARNING; expect new
  WARNING lines for conditions that were previously invisible, and read them as findings.
- The lint does not see a conditional report (a `raise` on one branch with a silent `else`) or a
  report hidden behind a helper that never logs; those remain review points.
