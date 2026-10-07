# A row's pid is checked for identity, not liveness

## Context

`agents_meta.pid` is an integer the agent process writes about itself. Nothing
holds a reservation on that number: once the process dies the OS may hand it to
anything, and after a reboot it hands out the whole range again, walking back over
every pid the previous boot's agents held.

Everything that consumed the column treated `shared/proc.py:process_alive` — a
`kill(0)` existence test — as the answer to "is the agent behind this row still
there". It is not the same question, and the gap is not academic. On 2026-07-30
(issue #1123) five prod agents sat `idling` behind pids that had been recycled to
live, unrelated processes. Both consumers failed in the same direction:

- the restarter's corpse reapers saw a live pid and left the rows standing —
  permanently, since a recycled pid never goes dead;
- the hibernation swap-out signalled those pids `SIGUSR1` once per restarter tick
  (five agents, 12-15 volleys each over about a minute) because a stranger obviously
  never performs the hibernate exit that would take the row out of the scan.

The second is the one that mattered. SIGUSR1's default disposition is
**terminate**. The controller was aiming repeated kill signals at whatever
processes happened to be holding those numbers, and its own docstring already
claimed it would "never signal a stranger" — an invariant nothing implemented.

## Decision

The predicate is process **identity**. `ops/agent_identity.py:probe_agent_process`
reads the pid's argv back from the OS process table and checks it is the agent's
own launch argv (`-m agent --agent-id <id>`), returning one of four verdicts —
`OWNED`, `FOREIGN`, `GONE`, `UNREADABLE`.

- Hibernation swap-out signals **only** `OWNED`.
- The two pid-based corpse reapers reap on `GONE` and `FOREIGN`, so a recycled-pid
  row now reaches `terminated`/`reaper` within one 30 s pass — which is also what
  stops it being re-selected by the swap-out scan.
- `UNREADABLE` — alive but its argv could not be read (another user's process, a
  zombie) — counts as resident. Absence of evidence must not drive a reap; this
  preserves the old "PermissionError counts as alive" safety floor by the same
  reasoning.

The identifying argv fragments are defined in `ops/agent_identity.py` and
**imported** by `ops/agent_launch.py` to build the argv, so the launcher and the
matcher cannot drift apart. A test drives the real launcher and feeds its argv
back through the matcher, because nothing at runtime would notice the drift: the
probe would quietly start calling every live agent a stranger and the reaper would
terminate the fleet.

Matching is deliberately restricted to the two fragments that have been in the
argv since agents had one, not the whole of it. During a rolling upgrade the new
restarter probes agents the previous version launched; matching on flags that come
and go would read every not-yet-restarted agent as `FOREIGN`.

## Alternatives rejected

**Treat `ESRCH` as "process gone" and reconcile the row.** The issue's other
candidate. It fixes nothing here: `os.kill` on a recycled pid *succeeds*. ESRCH
handling only covers the pid that stayed dead, which was already the benign half —
the reaper reached those rows anyway. It leaves both the stranded row and the
signal-a-stranger hazard intact.

**Stop at "hibernation refuses to signal an unverified pid".** Removes the
dangerous half and nothing else. A row whose pid is alive-but-foreign is invisible
to a liveness-based reaper forever, so the agent stays marked `running`/`idling`
while dead, is never resurrected, and the swap-out scan keeps re-selecting it every
tick. The strand had to be healed where "the row's process is gone" already lives.

**Record the process start time at claim and compare it to
`psutil.create_time()`.** The textbook pid-reuse guard, and strictly more general
than an argv match. Rejected because the two clocks are not the same clock: the
recorded value would come from Postgres (`now()` on the gateway host) and
`create_time()` from the runner's local boot clock, so any split deployment
compares across hosts and needs a skew tolerance — which is exactly the fudge
factor that makes such a check unreliable. It also costs a schema column and a
migration for evidence the process already carries in its argv.

**Verify against the native supervisor's session record.** The record is written
by the double-forking launcher; the DB pid is written by the agent process itself.
Cross-checking two independently-written pids adds a failure mode rather than
removing one.

## Consequences

- Both reapers now read the process table (`/proc/<pid>/cmdline`, `KERN_PROCARGS2`)
  instead of issuing `kill(0)`. Same O(fleet)-per-30 s shape, a heavier syscall;
  the 30 s cadence that already exists for exactly this reason absorbs it.
- The reaper can now terminate a row whose process is alive. That is the point,
  and it is why `FOREIGN` requires a cmdline that was read in full and `UNREADABLE`
  does not qualify.
- The agent's launch argv is now load-bearing beyond launching. Changing it means
  changing `ops/agent_identity.py`, and removing `--agent-id` or the `-m agent`
  form would need a replacement identity signal first.
- A pid whose argv is permanently unreadable is a permanent `UNREADABLE`, so such
  a row would strand as before. No such case is known on a runner, where agents and
  the restarter run as the same user.
