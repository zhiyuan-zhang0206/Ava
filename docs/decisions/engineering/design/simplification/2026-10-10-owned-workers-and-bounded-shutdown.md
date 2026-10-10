# Owned workers and bounded shutdown

Date: 2026-10-10
Status: Accepted

The user approved the three recommended choices below. These extend the
[2026-10-09 ownership decisions](2026-10-09-explicit-runtime-ownership-boundaries.md);
they are distinct decisions rather than a re-approval of that day's four choices.
Acceptance does not establish completed implementation or authorize deployment.
The user subsequently authorized removing explicit Gemini caching; that change
is implemented separately, rather than leaving cross-host cache sharing as an
unresolved product choice in this work.

## Explicitly owned threads

A Thread may remain when a real existing service or process owner admits the
work, retains its execution handle, provides its stop path, and collects
completion and original unknown exceptions. Stop closes admission; bounded join
reports any still-running work and identifies the existing final resource owner.
Independent watchdog protection and its established timeout classification
remain required until a replacement has been demonstrated equivalent.

The previous blanket Thread finding could not distinguish this ownership from
free-floating work. The selected lint direction uses visible structural wiring,
with meaningful consumer tests for the lifecycle properties AST cannot prove.
File allowlists, owner markers, uncalled cleanup methods and renaming work to
Timer, executor, helper or Thread subclass do not establish ownership. Existing
baseline entries retire only with verified component closure; a rule update
alone does not justify deleting all thread entries.

## Unknown errors after the caller returns

An unknown late worker error is recorded visibly as soon as it occurs, retained
by its original service owner and raised when that service stops or joins. Work
whose synchronous caller is still waiting propagates the error through that
caller. Shutdown preserves an already active primary exception rather than
replacing it with a later worker error; the secondary failure remains visible.
Merely logging an error does not fulfill the owner's propagation obligation.

The rejected alternative immediately failed a broader host or service whenever
any detached operation failed. That would change the failure impact and drain
order. Expected transport, SDK and exporter isolation remains explicitly scoped;
this choice does not convert those expected failures into business failures.

## Honest bounded best effort

Ordinary telemetry stops admission and uses its existing pipeline owner and
single drain writer. When that writer stalls or dies, the existing finite
shutdown budget may expire with unwritten events. The result explicitly reports
degradation and unfinished work; it does not acknowledge persistence that did
not occur. A second unowned rescue writer is removed. Hard exit does not promise
a flush. Business data and durable audit obligations are not relaxed.

Redis listeners retain request hard-return deadlines and have a separate finite
owner-stop budget. Deadline expiry reports known residual work honestly rather
than asserting that subscriptions, rollback and connections have all stopped.
The selected behavior does not add an unbounded join, a new supervisor or an
automatic process-kill mechanism. Late unknown errors follow the original-owner
receipt rule above, including immediate visibility after a bounded stop returns.

Current implementation and review requirements belong in
[Python conventions](../../../../conventions/python-conventions.md#ambient-state-inject-what-is-read-to-decide)
and the relevant component's documentation and tests.
