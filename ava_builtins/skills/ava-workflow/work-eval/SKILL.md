---
name: work-eval
description: Executes multi-step work with adversarial self-review and verification woven through every step; large tasks end with an independent adversarial review by a fresh agent. Use when implementation, research, or operations require continuous evaluation rather than a single final check.
---

# Work & Evaluate — The Evaluation Thread, Made Explicit

Evaluation is not a fourth phase that comes after Plan — it is a thread that
runs through **every** phase: "what counts as good" is answered by Align,
"how is reality doing" by Calibrate, "when and how do we check" by Plan. This
skill is that thread made explicit **during execution**: you execute, and you
evaluate — not as a final audit, but woven into every step, with each kind of
failure routed back to the phase that owns it.

```
        Calibrate ──┐        ┌── "is our model of reality still right?"
                    │        │
        Align ──────┼──→ EXECUTE ──→ evaluate ──→ "is the goal still right?" ──┐
                    │        │                                                │
        Plan ───────┘        └── "is the decomposition still right?"          │
                                                                              │
              ┌───────────────────────────────────────────────────────────────┘
              └── each failure routes back to the phase that owns it, then resumes
```

## The Network, Not the Final Step

The three phases aren't a staircase you climb once and leave behind. During
execution, evaluation reconnects to all three — and each connection has its
own failure mode and its own owner:

| Evaluation finding | What it means | Where it routes |
|-------------------|---------------|-----------------|
| "The step output doesn't meet its acceptance criteria" | Execution error | Fix here, re-execute |
| "The premise of this step is false — the system doesn't do what we assumed" | **Calibrate** failure | Re-run a calibration slice on that fact, then resume |
| "The goal itself was off — success criteria no longer make sense" | **Align** failure | Re-question the affected branch, update the alignment document, reconfirm |
| "The decomposition is wrong — steps too big, wrong order, missing pieces" | **Plan** failure | Re-split the remaining work, update the plan |

The self-challenge loop below is the mechanism; the table above is the routing.
**Never just "push on" past a finding** — a finding that belongs to another
phase, ignored, is the most expensive kind of error, because the work was
already misdirected before the step even ran.

## The Self-Challenge Execution Loop

This isn't just "follow the plan." After each step, switch to an
**adversarial perspective** — examine your work like a critical reviewer,
constantly asking: is this really moving us toward the goal?

```
Execute next step → Evaluate result → Self-challenge → Route & correct → Execute next step → ...
```

### Full Process for Each Step

#### 1. Pick the next step
From the plan, select the next executable task (dependencies satisfied,
highest priority). **With no plan** (the common case after Align), pick the
next slice of the goal that unblocks the most downstream work.

#### 2. Execute
Carry out the step as planned. Follow the project's coding conventions, use
the right tools.

#### 3. Self-evaluate
After completion, ask yourself:
- Does the output meet the acceptance criteria (from Plan's step, or from
  Align's success criteria when there's no plan)?
- Any edge cases missed?
- Is the quality at the expected level?

#### 4. Self-challenge (a weak signal)

Switch to an adversarial perspective and challenge your own work. This local,
cheap check catches step-level slips and is enough to gate a small task, but it
is structurally biased by the author's context and assumptions. It must never
be the final gate for a large task; use the independent review below.

- **"Is this really moving toward the final goal?"**
  - Does this step's output genuinely advance the goal? Or does it just "look busy"?
  - If the end goal is a house, is this step laying the foundation or picking paint colors for a roof that hasn't been designed yet?

- **"Is there a simpler way?"**
  - Did we introduce unnecessary complexity?
  - What would happen if we cut this step entirely?

- **"Am I solving the wrong problem?"**
  - Did we discover new information during execution that changes the original assumptions?
  - Do the original plan's premises still hold? **Do the alignment document's premises still hold?**

- **"What's the biggest risk here?"**
  - If this step goes wrong, how does it affect downstream steps?
  - Any edge cases or failure modes missed?

- **"If I were the user / reviewer, what would I be unhappy about?"**
  - Perspective-taking — what does the end user actually care about?
  - Does the output satisfy the success criteria in the alignment document?

#### 5. Route & correct

Based on the self-challenge:

| Situation | Action |
|-----------|--------|
| All good, direction correct | Update progress, continue to next step |
| Small issues found, fixable | Fix first, then continue |
| Step premise false (reality changed / was never calibrated) | **Route to Calibrate**: re-run the loop on that slice, update the record, then resume |
| Direction drift detected | Adjust subsequent steps in the plan (or, without a plan, re-order your own next steps) |
| Goal / success criteria no longer make sense | **Route to Align**: re-question the affected branch, update and reconfirm the alignment document |
| Decomposition wrong (steps too big, wrong order, missing pieces) | **Route to Plan**: re-split the remaining work, update the plan |
| Task complete | Final verification, produce delivery document |

### Record decisions

After each self-challenge, **briefly record your judgment** — this helps
with later review and lets the user understand your decision path:

```
## Step N Evaluation: [Step Name]

**Output**: [One sentence describing what was completed]
**Challenge**: [The most important self-challenge]
**Judgment**: [Why continue / why adjust / why route back to <phase>]
**Plan change**: [If any]
```

## Independent Adversarial Review

### When it is mandatory

Any one of these conditions makes the review mandatory:

1. **Long flow** — the task spans multiple sessions or days.
2. **Parallel work** — multiple agents or spawned workers produce results that join.
3. **High stakes** — the work is irreversible or outward-facing; involves real money; affects production, data, or security; or touches a system whose failure is expensive.
4. **The user cannot review in real time** — an autonomous batch needs the review agent as the user's stand-in.
5. **The Plan phase produced a plan** — Plan is reserved for large or parallel work, so its plan includes this review step.

For a large task, skipping independent review is not an option.
Self-challenge does not satisfy this gate. When in doubt, review: the pass costs
far less than a wrong delivery.

### Spawn a fresh reviewer

Use `ava.agents.spawn` to create a reviewer with no shared context and a
self-contained brief whose only job is to hunt for faults. Never use
`fork_from` the executor, because shared context carries the author's
assumptions into the review. Never use the author or any agent that worked on
the task. Give the reviewer the alignment document, plan, and artifact or diff
locations — not the author's justifications. The report must come from the
artifacts.

### Review target and report

Review:

- the delivered work against every success criterion from Align;
- every plan step against its acceptance criteria;
- failure paths, not just the happy path, plus edge cases; and
- the routing history: were findings sent back to the phase that owned them,
  or papered over during execution?

Use this output shape:

```markdown
Verdict: APPROVE | REQUEST CHANGES

P0/P1/P2: [finding]
Location: [file:line]
Evidence: [what proves the defect]
Suggested fix: [specific correction]
```

Use the [`adversarial-review`](../../../../.agents/skills/adversarial-review/) skill for the
hunting stance: assume the work is defective, prefer the failure path, and
apply severity discipline. A finding without a location and evidence is not a
finding.

### Interact until clean

One round is:

1. The reviewer works independently and posts the report through
   `ava.agents.send_message`, or writes it to a file named in the brief.
2. Fix every P0/P1 or rebut it with evidence.
3. The same reviewer re-checks only the changed hunks and rebutted findings.
4. Repeat until the verdict is clean.

The author cannot waive P0/P1; only the user can. Escalate any P0 that survives
rebuttal to the user.

### Close out

Record the verdict in a task-log line or PR comment so the trail proves the
gate passed. Deliver or merge only after a clean verdict, then terminate the
review agent as a one-shot worker. If the review routes a finding back to
Calibrate, Align, or Plan, run that loop and re-review before delivery.

When the user is unavailable during an autonomous batch, the reviewer is the
user's stand-in — mandatory, not optional. The batch's single authorization
covers execution; this review checks that the result matches what the user
authorized.

## Loop Termination Conditions

The loop terminates when ALL of the following are met:
1. All steps in the plan are complete (or, without a plan, all slices of the goal are done)
2. All success criteria are satisfied (check against alignment document item by item)
3. The appropriate final gate passes: a small task's self-challenge finds no substantive flaw; a large task has a clean independent-review verdict

## Delivery

When the task is complete:
1. Collect all outputs (file paths, PR links, etc.)
2. Verify completion against the alignment document item by item
3. Write a brief summary including key decisions and known limitations
4. Notify relevant parties (user / peer agents)

## Standalone Use

Work & Evaluate can be used independently — if you already have a clear goal
and acceptance criteria, jump straight into the execution loop. However,
**strongly recommended** to maintain the self-challenge habit even in
standalone use — and to keep the routing table in mind: a failure found during
execution belongs to whichever phase caused it, even if that phase never ran
formally. The thread exists whether or not you climbed the staircase.

## Don't

- Don't skip the self-challenge — this is what distinguishes the workflow from "mindless execution"
- Don't challenge for the sake of challenging — question substance, not nitpicking
- Don't push forward when you've found a direction error — admitting a mistake and adjusting course is more efficient than sticking to a wrong plan
- Don't ignore small problems — small issues compound into big ones by the end
- Don't treat evaluation as a final gate — a wrong goal, an uncalibrated premise, or a bad decomposition discovered at the end costs far more than when discovered mid-step. Route early, route often.
