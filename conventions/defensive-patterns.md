# Defensive patterns

Hard-won bug-class rules. Every pattern here is a class of defect that actually
shipped, or nearly shipped, in this repo — not a general best-practices list.

**Read before** writing lifecycle, release, or infrastructure-touching code,
before adding a protective test or lint, and before acting on a diagnosis.

Each entry is a rule plus a pointer to the evidence. The narrative — what broke,
why every safety net missed it — lives in `postmortems/`; this page is the
distillation, and it is the half people actually re-read. The pipeline runs one
way: a postmortem produces guardrails, and the guardrails that generalize condense
into an entry here. See [`doc-maintenance.md`](doc-maintenance.md) for how the two
sit among the other axes.

## Release and long-lived processes

### A rollout cannot deliver its own protection

The first deployment of any safety mechanism is unprotected by that mechanism.
Plan the manual and verification steps of such a rollout as if the old code were
running, because it is; assume the new safeguard is live from the *next* rollout.
Check before relying on one: `git cat-file -e <old-sha>:<path>`, or grep the
commit you are deploying **from**.
Evidence: [`postmortems/0001`](../postmortems/0001-a-rollout-cannot-deliver-its-own-protection.md).

### A running process does not adopt a tree checked out underneath it

A daemon, watchdog, orchestrator, or agent keeps executing the code it imported.
"The file on disk says otherwise" is not a rebuttal. This is why the orchestration
executing any given rollout is always the old code, and why new orchestration
behavior takes effect one rollout later than it lands.
Evidence: [`postmortems/0001`](../postmortems/0001-a-rollout-cannot-deliver-its-own-protection.md).

### A flag cannot fix the rollout that introduces it

A parameter only arrives if the caller knows to pass it, and on the deciding
rollout the caller predates the parameter. When a leg needs a new fact, design it
to **read the fact itself** rather than be handed it — the same fact reached
entirely inside the new code. Worked example:
`cli/commands/start.py:_readiness_waiver`, which observes the update lease instead
of waiting for a `--flag`.
Evidence: [`postmortems/0001`](../postmortems/0001-a-rollout-cannot-deliver-its-own-protection.md).

### Isolation that one command can undo is a convention, not a boundary

If a single operational act — restarting a service, dropping a database, clearing
a cache — can take out every tenant, the tenants were never isolated; they were
co-located under a rule someone had to keep. Discriminators held correct by
configuration (a database name, a logical-DB index, a channel prefix, an ACL user)
are that rule wearing a schema. Prefer the structural version: a separate
instance under a separate home, so the blast radius is set by what the thing *is*
rather than by how carefully the operator aimed. This is why every Ava cluster
owns its own Postgres and Redis.
Evidence: [`postmortems/0004`](../postmortems/0004-isolation-one-command-can-undo.md).

## Tests and guards

### A dead dependency is a fact about the dependency, not about one seam

A test asserting behavior under a downed dependency must close **every** route to
it, not the route the current implementation happens to take. Patch `connect`
*and* `pool`; otherwise the fixture silently retargets itself the next time the
implementation changes seam, and the test keeps passing while guarding nothing.
Evidence: [`postmortems/0002`](../postmortems/0002-db-down-tests-pass-for-the-wrong-reason.md),
and the `no_db` / `dead_db` fixtures in `tests/services/test_healthcheck_restarter.py`.

### A real-infrastructure suite fails open

A mocked suite fails closed: an unmocked call blows up. A suite that provisions
real infrastructure — as Ava's does, a throwaway Postgres per session — fails
*open*: an unmocked call succeeds against an empty table, and "it returned
nothing" is not "it could not reach the database". Reason about absence
explicitly; the environment will not do it for you.
Evidence: [`postmortems/0002`](../postmortems/0002-db-down-tests-pass-for-the-wrong-reason.md).

### A guard only guards if the regression actually fails it

When adding a protective test, lint, or assertion, introduce the regression it
targets, watch it go red, and revert — in the same PR. A green result from a test
that cannot go red is indistinguishable from a green result that means something.

**Revert by sha, not by stash.** `git stash push <implementation file>` stashes
only *uncommitted* changes, so once the fix is committed — the usual case for a
test written against a fix that already landed — it silently stashes nothing, the
implementation stays in place, and the test passes. That reads as "I proved it
red-before" while proving the opposite, which is worse than no proof because it
manufactures confidence. Use `git checkout <sha-before-the-fix> -- <file>`, re-run,
then `git checkout HEAD -- <file>`; confirm the revert actually landed (`git diff`,
or grep the file for a token from the fix) rather than trusting the command.
Evidence: [`postmortems/0002`](../postmortems/0002-db-down-tests-pass-for-the-wrong-reason.md);
procedure in [`.agents/skills/run-local-tests/SKILL.md`](../.agents/skills/run-local-tests/SKILL.md).

### Verify the world, not the self-report

An end-to-end assertion must re-run the command or re-read the file **externally**,
and assert that untouched files are byte-identical. Never grep an agent's own
output for success claims: an agent that merely *says* it did the thing passes a
keyword probe, and so does one that did the thing wrong and narrated it well. The
report is the thing under test, not the evidence.

### The blast radius is where the consumers' guards live

Not where the diff's lines are. `shared/` sits at the bottom of the import
layering, so every layer above consumes it — and this repo deliberately places
exhaustiveness assertions over enums and field sets in the **consumer's** test
file, as review forcing functions. Edit-adjacency is structurally blind to them.
Editing anything in `shared/` means the full suite.
Evidence: [`postmortems/0003`](../postmortems/0003-touched-areas-is-not-the-blast-radius.md).

### Prefer a mechanical guard where the boundary is nameable

A rule someone has to remember loses to a hook that fails with a clear message.
`scripts/lint_note_tags.py` turns one arm of the enum blast-radius class into a
pre-commit failure; derived sets (`agent/state.py:_BASE_STATE_FIELDS`, from
`model_fields`) need no guard at all because they cannot go stale. Reach for the
written rule only where the boundary genuinely resists naming.
Evidence: [`postmortems/0003`](../postmortems/0003-touched-areas-is-not-the-blast-radius.md).

### A guard that looks redundant is the one that catches the fix

Two producers of the same fact, pinned against each other, feel like a test of
something nobody would get wrong. `tests/shared/test_cluster_env.py:test_health_port_env_matches_derive_env_for_the_same_base`
pins `derive_env` (install-time) against `health_port_env` (enroll-time) for one
base — and what it caught was not the original bug but the FIX for it: adding
`agent_host` to `_LATE_HEALTH_SLOTS` made the two producers disagree, and the
guard said so immediately.

Two lessons, and the second is the load-bearing one:

- A table that is not derived needs a guard for its own internal invariants, not
  just for agreement with its consumers. `LEGACY_AVA_PORTS` is not in offset
  order (`ops` moved off 8106 to dodge a Windows service), so "next number after
  the last line" put `agent_host` on a port `ops` already held; the entire suite
  passed, because nothing asserted the table had no duplicates. That collision
  surfaced from reading a boot log, not from a test —
  `test_legacy_ports_are_unique` exists now so the next one does not need a
  careful reader.
- The value of a cross-producer guard is highest exactly when you are changing
  the thing it guards. Deleting one because "both sides obviously agree" removes
  it at the moment before it would have earned its keep.

Evidence: PRs #169 and its follow-up fix, where a port-offset addition produced
three distinct collision classes — a derive escaping its own record's block, an
overlap with the next cluster's block, and a duplicate legacy port.

### A health check that does not depend on what it certifies stays green through the outage

A check whose code path shares nothing with the path that breaks will keep
answering 200 while the system is down — and its greenness is then actively
harmful, because it is the thing people consult before believing the alarm. The
gateway's HTTP health does not authenticate to Redis, so it certified a healthy
cluster for the fifteen minutes every agent on it was dead. Make a check exercise
the dependency it vouches for, or scope its claim down to what it actually
touches.
Evidence: [`postmortems/0004`](../postmortems/0004-isolation-one-command-can-undo.md).

## Debugging

### A credentials error is not a server error

`WRONGPASS`, `AuthenticationError`, a 401 — each names the credential that was
presented, not the health of what rejected it. No restart of any server changes
what a client sends, so restarting is not a cheap thing to try first: it has no
causal path to the symptom, and its one reliable effect is on whatever state the
server holds only in memory. Read the error as the statement it is, then find the
config that holds the wrong value.
Evidence: [`postmortems/0004`](../postmortems/0004-isolation-one-command-can-undo.md).

### A mechanism that predicts the symptom is a hypothesis, not a diagnosis

A causal story that accounts for what you observed is not thereby the cause —
several will. Before acting on one, name the cheapest experiment whose outcome
differs between your mechanism and the alternatives, and run it. It is usually one
command: set the variable, start the missing daemon, shorten the path. The tell
that you have skipped this step is a fix justified by "this would explain it"
rather than by an observation. Same discipline as *a guard only guards if the
regression actually fails it*, applied to diagnosis rather than to test-writing.
Evidence: issue #77 — two mechanisms proposed for one failing test, both
predicting the symptom, both wrong. "It needs a tmux server" was disproved by
starting one (the module under test contains no tmux at all); "the pytest tmp
counter reached three digits" was true and still incomplete, since `TMPDIR` is a
second, independent input — `TMPDIR=/tmp pytest ...::test_new_honors_cwd` passes
(69-character path) where the default per-session `TMPDIR` fails (121).

### Agreeing on the failing test names is not agreeing on the failure

Rival mechanisms usually agree on *which* tests fail and disagree on *what the
failure looks like*. Discriminate on the observed values, never the names: find
the branch that produces those values and ask which mechanism can reach it. Then
**force** the loser rather than call it unlikely — making the suspect call fail on
every invocation costs seconds and turns "probably not that" into "provably not
that". A mechanism you merely argued down stays available as an explanation for
the next thing that breaks nearby.
Evidence: issue #147 — two mechanisms offered for the same two failing tests. The
reported values (`goto: after_exec` *and* zero emitted events) arise only where
`_llm_repair_syntax` returns `None`, which the tests' own `AsyncMock` makes
unreachable, leaving one candidate. The rival (a `ruff` subprocess exceeding its
5s budget under load) was then forced by raising `subprocess.TimeoutExpired` on
every `ruff` call: both assertions still passed. Two seconds of work, against a
47-minute suite run that had suggested it.
