```markdown
# Unify report + question into one `notice` primitive

## Context

The agent→user surface had grown two near-identical primitives, each with its own
table, SDK verb, endpoints, events, and snapshot projection:

- `ava.ui.ask_question` → `agent_questions` (needs a response; `blocking` bool;
  answered_at/answer + closed_at/closed_by).
- `ava.ui.submit_report` → `agent_reports` (FYI; no blocking, no answer; read_at +
  closed_at).

The two tables shared `id / agent_id / title / content / priority / created_at`
and differed only in `blocking`, the close columns, and the transport (open
questions ride the agent snapshot inline; reports stay off it as a count + feed).
The duplication was real machinery: two routers (6 endpoints), two event roles,
two Pydantic model families, two frontend fetch paths. `supervision-queue.md` (since removed)
had explicitly **rejected** unifying them ("don't merge into one `agent_messages` + `kind`
列"). This entry reverses that.

## Decision

One table `agent_notices`, one create verb `ava.ui.notify`, discriminated by a
`require_response` bool. The primitive is one object at three obligation rungs;
`priority` (P0..P3, stakes) is an orthogonal axis:

| require_response | blocking | rung |
|---|---|---|
| False | False | FYI — the user may glance or ignore |
| True  | False | needs an answer, agent keeps working |
| True  | True  | needs an answer, agent is stalled |

`blocking ⊂ require_response`: you can only be stalled waiting on a reply you
actually need, so `(require_response=False, blocking=True)` is incoherent and
rejected three ways — the SDK `notify` raises `ValueError`, a table CHECK forbids
it, and `submit`-style FYI calls simply default `blocking=False`.

Resolution collapses to `resolved_at + resolution (answered|dismissed|read|
withdrawn) + reply`. The `reply` column **caches** the user's free-text reply for
history/display; live delivery to the agent still rides the chat-inbound path —
the column is not the delivery channel. The two user-facing queues survive as two
*views* over the one table (`WHERE require_response`), keeping the load-bearing
transport asymmetry (snapshot-inline bounded worklist vs off-snapshot FYI feed)
as a predicate rather than a second table. Six endpoints converge to three:
`POST .../notices/{id}/resolve` (explicit `action`), `GET /api/notices/open`
(FYI feed), `GET /api/notices/resolved` (history). Two new agent verbs land on
the way: `ava.ui.edit_notice` (revise an open notice) and `ava.ui.list_notices`.

## Alternatives rejected

- **Keep the two tables / two SDK verbs.** The split was the very thing that
  duplicated the machinery; the two objects are one primitive at different rungs.
- **Two free bools `require_response` + `blocking` exposed on one function, with
  the illegal combo merely validated.** Rejected during design in favour of the
  ladder being structural; kept as a single `notify(require_response, blocking)`
  per the operator's call (one primitive = one create function), with the illegal
  combo raising `ValueError` at the call site (the agent self-corrects) — but
  reports never carry blocking because that quadrant does not exist.
- **A single 3-value ordinal column** (`attention: fyi|reply|blocked`) instead of
  two bools + CHECK. The operator chose the two-bool encoding to match the SDK
  signature 1:1; the CHECK makes the illegal state unrepresentable either way.
- **`agent_messages` + a `kind` column with everything nullable.** That is what
  `agent_notices` is, but with explicit CHECKs (blocking⊂require_response,
  resolution legality, answered-has-reply) so illegal states can't be written —
  not a loose bag of nullable columns.
- **A "blocking report" (urgent FYI).** "P0 incident, glance please, no reply" is
  `priority=P0` + `require_response=False`; loud interruption is a separate
  notification concern, not a new meaning for `blocking` (which must stay
  self-verifying: an agent claiming blocking while still working is visibly lying).
- **Inferring answered-vs-dismissed from whether `reply` is present.** "Dismiss
  with a note" would look like an answer; the close verb is stored explicitly
  (`resolution`), never inferred.

## Consequences

- One data migration (`0053`, create+copy+drop). Reversible via a paired
  `.down.sql` that rebuilds both old tables and copies rows back; safe to drop in
  one migration because `ava update` pauses agent-runners before migrating, so no
  old-code process writes the old tables across it.
- The SDK now hands the agent a small bool matrix on one call instead of two
  named verbs — accepted in exchange for one primitive / one name end to end.
- Reports can now receive a user reply (cached + delivered), where before a report
  reply was delivered but not stored.

Supersedes the "don't unify" stance in `future/supervision-queue.md` (since removed);
builds on [`2026-06-14-ask-user-primitive.md`](2026-06-14-ask-user-primitive.md)
(the priority×blocking axes and fire-then-idle model are unchanged).

Updated by [`2026-08-02-notice-sdk-slimming.md`](2026-08-02-notice-sdk-slimming.md):
`list_notices` removed, `edit_notice`/`dismiss_notice` lose the id argument
(at most one notice is open per agent), `self.log` removed.
```
