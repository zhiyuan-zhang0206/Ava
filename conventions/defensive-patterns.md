# Defensive patterns

Hard-won bug-class rules. Every pattern here is a class of defect that actually
shipped, or nearly shipped, in this repo — not a general best-practices list.

**Read before** writing lifecycle, release, or infrastructure-touching code, and
before adding a protective test or lint.

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
targets, watch it go red, and revert — in the same PR. For a test written against
an existing fix: `git stash push <implementation file>`, re-run, confirm *that*
test fails, `git stash pop`. A green result from a test that cannot go red is
indistinguishable from a green result that means something.
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
