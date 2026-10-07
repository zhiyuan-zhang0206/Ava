# The updater marks where its run begins, rather than getting its own log file

## Context

`ops.updater_outcome` reads a stalled host's updater log to separate the two
readings `POLL_STALLED` covers: a preflight that **refused** (nothing stopped,
host intact and serving its old code) from an updater that **died** after moving
the checkout. Opposite next actions, so guessing is worse than not answering.

Which lines belong to *this* update was answered by dating the file: a log last
written before the current pause is a previous update's and reports as no record.
That works on POSIX, where every spawn tees to a fresh `updater-<epoch>.log`, so
one file holds exactly one run.

Windows has no per-run file. The native supervisor owns the redirect and appends
every run to one `ava-updater.out.log`, so the pause flag dates the FILE and not
the lines in it: a bounded tail can open in the middle of an earlier run, and the
decline marker — the only verdict Windows writes at all, since the cmd.exe
command line emits no `[session-exit] rc=` line — is matched anywhere in that
tail. A host half-transitioned by a newer run was reported as untouched and still
serving. The wrong half of the exact distinction the reader exists to draw, on
the platform it exists for.

## Decision

The updater's cmd.exe command line echoes a per-run start marker
(`[updater-run] <spawn-epoch>`) ahead of everything else it does, and the reader
slices the tail at the last such marker not older than the current pause.

**Where in the line the marker may sit is decided in two passes, and the order is
the safety property.** The primary rule is column zero, which is where `echo` puts
it and where the output this reader does not control does not go unprefixed
(`git checkout` writes `HEAD is now at <sha> <subject>`; `uv sync` indents its
package lines). Only when that finds nothing does a second pass accept a marker
anywhere in a line — and only carrying an epoch no older than the pause.

The loose pass exists for one real shape: a previous run force-killed mid-write,
which is precisely what the hung-updater reaper does. Its last line reaches the
shared file without a newline, so this run's marker is appended into the *middle*
of it and column zero is structurally unreachable for that run. Ordering the
passes rather than replacing the rule is what keeps both properties at once — the
killed-run case anchors, and marker-shaped text in command output still cannot
decide which lines a host is judged by, because such text carries an old epoch or
none. What is left is a subject embedding a *future* epoch: adversarial rather
than accidental, and worth at most a narrower diagnostic window on a host already
being updated.

Emitter and parser live in one module (`ops.updater_outcome.mark_native_run` /
`_anchor_to_this_run`): they agree on more than a string — how the marker is
printed is what the primary rule reads — and a marker spelled or printed
differently is a marker nothing anchors on, failing silently as the
misattribution it was added to prevent.

A tail where neither pass finds a marker is read exactly as before, and that is
sound rather than a fallback: the marker is the run's first line, so its absence
means the run has already written more than `_TAIL_BYTES`, and a tail that far
into one run cannot reach back into the previous one. The one case outside that
argument is an updater spawned by code predating the marker — the rollout that
ships it — which reads as it always did.

POSIX prints no marker and is left byte-identical. Its log already holds one run;
a marker there would separate nothing and would be one more line in a file
operators read.

## Alternatives rejected

- **Per-run log files on Windows too** (the other direction issue #1117
  sketches). It removes the ambiguity at its source rather than annotating around
  it, and it would make the `{"log": ...}` handle `spawn_update` returns name a
  file that exists on that platform. Rejected because the redirect is not the
  updater's to choose: `shared/winproc.py` opens `$AVA_HOME/logs/<session>.out.log`
  for **every** session it supervises, so per-run naming is a change to the
  supervisor's contract with agents, daemons and shells alike — a far larger blast
  radius than the reader that asked, and one that moves where an operator looks
  for a Windows session's output. The marker leaves the log channel exactly where
  every other consumer expects it.
- **Accept a marker anywhere in a line, full stop — no column rule.** One pass
  instead of two, and it handles the force-killed-mid-write case directly rather
  than as an exception. Rejected as the *only* rule because it hands every line of
  command output a way to end the window: `git checkout` echoes a commit subject
  verbatim, so a subject containing the marker literal would be enough to truncate
  the tail to whatever followed it, and the epoch is then the sole lock on text
  that an operator of the repo being deployed writes. Ordering the two passes
  costs a loop and keeps column zero — where the marker actually is on every
  healthy run — as the rule that decides the ordinary case.
- **Anchor on the last marker, full stop — no epoch.** Simpler, and it handles
  the ordinary case identically. Rejected because a marker is only evidence if it
  was written after this update paused the host: anchoring on an older one starts
  the slice inside the previous run and hands back its whole output as this
  update's — the same misattribution, harder to see, and now wearing the
  authority of having been "anchored". The epoch is also what makes the marker
  hard to forge, since a line that merely looks like one carries whatever stamp it
  was written with.
- **Byte offsets recorded at spawn instead of a marker in the band.** The spawner
  could remember where the file ended and the reader could start there. Rejected:
  the spawner and the reader are different processes on different sides of a
  restart — the whole reason this module reads a log instead of being told — so
  the offset needs somewhere to live that survives the update, which is a state
  file with its own staleness problem. The marker travels in the one channel that
  is already proven to survive.
- **Making the reader answer "unknown" whenever it cannot anchor.** Safe-looking,
  and wrong: it would report nothing for every Windows run whose output exceeds
  the tail bound, which is the long ones — the runs most worth diagnosing.

## Consequences

- The Windows updater log gains one line per run. It is also the only thing in
  that file that says where a run starts, so it doubles as the seam an operator
  reads it by.
- Windows attribution is now sound for any run whose output fits the tail bound,
  and no worse than before for the rest. `_TAIL_BYTES` and the marker are a pair:
  the two cases are exhaustive at any value of the bound, but lowering it shrinks
  the set of runs that anchor precisely.
- Windows-only behaviour that this repo's CI cannot execute. The marker's
  emission is pinned as a command **string** and the slicing as pure text; that
  cmd.exe prints `echo <marker> & <rest>` as its own line is the assumption a real
  Windows box has to confirm.

<!-- The run marker stands. One premise above no longer holds: the cmd.exe chain
does emit a `[session-exit] rc=` line as of
decisions/2026-08-12-a-written-ending-outranks-the-updater-lease.md, so the decline
marker is no longer the only verdict Windows writes. -->
