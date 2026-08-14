# The ask-user primitive: a flat priority queue, not a chain or a questionnaire

## Context

An agent that hits a decision only the human can make needs a clean way to ask
and to surface the question without drowning the human. Two forces shape this:

- **Authority for non-delegable decisions is concentrated at one node** — the
  user. No intermediate agent can authorize "send this email to a real person" /
  "spend this money" / "submit this form." The spawn tree governs agent↔agent
  delegation; it has nothing to forward such a decision *to* except the user.
- **The human's one scarce asset is attention.** Reachability is free — any agent
  at any depth can address the user — so the hard problem is not delivery but
  triage: making a cross-fleet stream of questions sortable so the user spends
  attention where stakes are highest.

The prior promise — "message the human and idle when you want a human call" —
was a dangling reference: the user has no agent row, and `send_message` requires
an `agent_id`. There was no working channel at all.

## Decision

A single core primitive, `ava.ui.ask_question(...)`, paired with `ava.ui.show` — asking
the user is about the *user*, not the agent. It is **fire-then-idle**: it posts a
question and returns; the agent ends its turn and idles; the answer arrives later
as a chat inbound that wakes a fresh turn. It cannot block like `input()` — a turn
runs `execute_code` to completion with no event loop to deliver an answer
mid-execution, and a blocking call would pin the turn open and break idle/cancel.

A question carries a `title`, optional long `content`, and **two orthogonal axes**
(Eisenhower):

- **priority** (P0..P3) = *stakes*. Subjective and inflation-prone, so
  **manager-calibrated**: the asker's self-estimate, adjusted by its manager in
  the one-hop consult that also decides resolve-or-forward. It is a
  *recommendation*; the user does final triage.
- **blocking** = *is the agent stalled waiting*. Objective and self-verifying (an
  agent that claims blocking but keeps working is visibly lying), so the manager
  cannot override it.

They stay two axes rather than one priority scalar because the
"blocked-trivial vs non-blocking-important" call is exactly what the user should
make by eye.

Two channels, split by who can settle the question:

- **Delegable** → `send_message` up the spawn tree; the chain absorbs what it can
  resolve, the user never sees it.
- **Non-delegable** → `ava.ui.ask_question`, flat, from any depth.

"Ask your manager first" survives as **subsidiarity** (resolve at the lowest
competent level), not a mandatory full-chain walk. Its real job is
*classification*: a leaf often can't tell whether a question is non-delegable; the
manager either resolves it or certifies "only the user can decide." A manager that
would obviously just forward is skipped; a confident leaf goes direct.

When an answer is a *generalizable policy* ("only send to pre-approved
addresses"), the agent propagates it up the chain / into memory, so the class is
resolved by the chain next time — the user is asked once, not every time.

The answer stays **pure free-text**. Multiple choices are enumerated A/B/C *in the
content* (a convention) so the user can reply with one letter, but nothing is
parsed.

## Alternatives rejected

- **Org-chart escalation — a question walks hop-by-hop up the spawn tree to the
  user.** A category error from human orgs, where a hierarchy fuses routing,
  filtering, and authority into one ladder *because* authority is distributed
  across levels and the top is unreachable. Here authority is concentrated and
  reachability is free, so every hop can only forward — pure latency. What
  survives, protecting scarce attention, is done better by a priority-sorted queue
  than a routing tree.

- **A typed questionnaire — `options: list[str]`, `multi_select`, per-question
  `reject`, clickable choice buttons, a batch of questions per ask.** The earlier
  shape. Turning free-form options into buttons needs an LLM in the frontend and
  reopens the whole single-vs-multi-select / "other" / no-selection mess. The
  A/B/C-in-content convention buys the brevity without the machinery.

- **Forcing all asks through `ava.ui.show` (rich HTML pages).** `ava.ui` can
  render any panel, but it optimizes *producer expressiveness* while the binding
  constraint is *consumer triage bandwidth*. Fifty bespoke pages have no shared
  schema, no priority, no dedup — un-triageable, and an HTTP server per question is
  heavy. The primitive earns its keep on the consumer side: a canonical,
  low-entropy, priority-tagged shape so the single queue is sortable. Maximum
  expressiveness and low-entropy-canonical are opposites; the attention bottleneck
  wants the latter. `ask` joins `ava.ui` as a lightweight sibling of `show`.

- **Modeling the user as a code-executing agent (`agent_id=0`).** Breaks the
  conceptual model — the user is not an agent — and pollutes every agent-list
  surface.

- **A new inbound kind for answers, plus `escalation_id` matching on the agent
  side.** The answer is just a chat message with routing metadata; the claim node
  should not grow a branch for it. The frontend already answers a *specific*
  question by id, and a self-describing inbound (`Re: "<title>" → <answer>`) tells
  the agent which of its open questions this settles — machinery managing an
  ambiguity that barely exists.

- **Letting the agent poll question status (`list_pending`).** Wrong semantics —
  state changes must wake the agent via inbound, not be polled.

## Consequences

- The only genuinely new mechanism is the **user-side queue**. Everything else
  reuses existing primitives: `ask` writes a question row and publishes the
  generic `agent_updated`; the snapshot carries `open_questions`, so the frontend
  gets the cross-fleet queue with **no new event and no new fetch** — it flattens
  open questions across the agent list it already holds. The answer rides the
  existing chat-inbound path.

- The asker must reconstruct *which* question an answer settles from the
  self-describing inbound and its own turn memory; there is no answer-side typed
  linkage.

- Free-text answers mean the agent parses replies itself — accepted in exchange
  for never building a frontend choice parser and keeping the answer channel
  dumb.

- The chain is restricted, not abolished: it resolves what it can and is expected
  to grow more competent over time as policies are fed back, but the user remains
  the single consumer for everything non-delegable.

- The surface lives in the supervisor's fleet view, split: the agent tree ("what
  is the org doing") beside the priority queue ("what does the org need from me"),
  the queue primary and flat, the per-node badge secondary (where to install a
  standing policy).

- No timeouts, no deadlines, no GC: a pending question waits until answered or the
  agent closes it. Each question is bound to a single agent — no cross-agent
  broadcast.

---

**Update (2026-06-17):** `ask` is no longer a *core* `ava.ui` member. It moved
into the `ava_fleet` plugin (still exposed at `ava.ui.ask_question`), so it strips together
with the rest of the human-supervision surface (`ava.self.log` / `ava.ui.submit_report`).
The reclassification — the strippability circle's center is "is a human
supervising," not `ask` itself; with no human, `ask` has no one to answer it — is
argued in `future/supervision-queue.md` §2 (since removed).
The behavioral design above (fire-then-idle, the two axes, the flat queue,
free-text answers) is unchanged; only its home and strippability moved.
