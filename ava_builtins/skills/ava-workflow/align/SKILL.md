---
name: align
description: "Aligns agents and humans on the real goal, constraints, priorities, decisions, and definition of done. Use when a task states what but not why, leaves meaningful choices open, or the user asks to be questioned or grilled."
---

# Align — Sync Intent Between Agents and Human

Don't start building on vague requirements. Invest a few turns asking questions to surface implicit assumptions, constraints, and success criteria. The sole output of this phase is **a clear alignment document** — not code, not a solution design.

The goal is to resolve every branch of the **decision tree** — surface each open fork, make an explicit choice, and close it — until no ambiguous branch remains.

## What Align syncs

Align calibrates **intent** — what the human wants and what the agents will do — rather than calibrating facts (that's Calibrate's job; see below). The alignment document is the contract that both sides sign: it says what "good" means, what's off-limits, and what ranks first. **The success criteria in this document are the evaluation standard for everything that follows** — evaluation first shows up here, not at the end.

## Core Idea

When a user says "build me an X," roughly 80% is still in their head: why, who it's for, the deadline, what "good" means, what's off-limits. Your job is to **surface that 80%**.

Inspired by Matt Pocock's ["grill me" skills](https://github.com/mattpocock/skills): the agent shouldn't passively accept vague instructions. Instead, actively question the user — challenge assumptions, uncover context, expose risks. This applies to **any** task — coding, research, writing, analysis, ops changes, even personal decisions. The technique is the same; only the domain vocabulary changes.

## Process

### 0. Explore the environment before asking

Before firing any question at the user, ask yourself: **can I answer this by looking?**

- Is the answer in the codebase? (Read files, search for related code, check git history)
- Is it in the project docs? (AGENTS.md, CONTEXT.md, conventions/, memory pool)
- Is it an observable fact? (Check running processes, config files, deployed state)

**Facts come from the environment. Decisions come from the user.** Don't ask "what database do we use?" when you can grep the connection string. Do ask "should we use the existing table or create a new one?" — that's a decision only the user can make.

**When the user's model of the subject itself is off**, that's not an Align problem — it's a Calibrate problem. Stop aligning, run a quick calibration slice on the contested fact, then resume. Align on top of a wrong model produces a confident alignment document that is wrong anyway.

### 1. Receive the task, quickly scan for missing information

After reading the task description and exploring the environment, immediately ask yourself:
- Do I know **why** we're doing this? (What's the underlying problem?)
- Do I know **who** the end user / beneficiary is?
- Do I know what **success** looks like? (Concrete, verifiable criteria)
- Do I know the **constraints**? (Time, resources, tools, platform, things not to do)
- Do I know the **priorities**? (If there are multiple goals, which ranks first?)
- Do I know the **context**? (What's the background? What has been tried before?)
- Are there **open decision branches** I haven't identified yet?

### 2. Build a decision tree and work it in rounds

Map the open decisions as a **design tree**: every decision branches into the decisions that hang off it. Then work the tree in **rounds**.

The **frontier** is every decision whose prerequisites are already settled — the questions you can ask *now* without guessing at answers you haven't heard yet. Each round:

1. Ask the whole current frontier at once, numbered.
2. A question whose answer depends on another question still open *this* round belongs to a *later* round — hold it back.
3. Wait for the user's answers. Each answer settles a decision and pushes the frontier outward, unblocking the questions that depended on it. Recompute the frontier and ask the next round.

Keep each round compact (aim for ≤5-8 questions), but the *session* runs until the frontier is empty — every branch visited, nothing silently assumed. **Order rounds by dependency**: a decision's prerequisites are answered before the decision itself is asked. Importance only orders questions *within* a round, never across them.

A useful checklist for what the frontier's earliest rounds should surface:

1. **Goal & motivation** — "Why do this? What problem does it solve?"
2. **Success criteria** — "How do we know it's done? What conditions must be met?" (These become the evaluation standard.)
3. **Constraints & boundaries** — "Deadline? Budget? Platform restrictions? Things we absolutely cannot do?"
4. **Priorities & trade-offs** — "If time runs short, what can we cut first?"
5. **Context & history** — "What's been tried before? Why didn't it work?"

**Every question carries your recommended answer — no exceptions.** This is the heart of the technique. Committing to an answer first forces you to actually think it through; it lets the user just accept or reject instead of composing from scratch; and any gap between your recommendation and their reality surfaces on the spot. A question with no recommendation attached is one you haven't done your homework on — don't ask it yet.

**Fact reconnaissance during grilling is non-blocking.** When a frontier question turns on a *fact* you could look up rather than a *decision* only the user can make, dispatch a sub-agent (`ava.agents.spawn`) to find it — never ask the user for something you could discover yourself. Don't stall the round on it: a running lookup is just an unsettled prerequisite, so only the questions *downstream* of that fact wait for the sub-agent to report — ask the rest of the frontier now. (This is the dynamic counterpart to §0: §0 is the static sweep before you open your mouth; this is the reconnaissance you spin up mid-interview as new unknowns appear.)

### 3. Questioning technique

- **Be specific. Ask about scenarios.** Don't ask "do you want high code quality?" (the answer is always yes). Ask "if test coverage and delivery speed conflict, which do you choose?"
- **Offer options, and bet on one.** Don't ask "how do you want to do this?" Give 2-3 concrete directions *and* commit to a recommendation: "Option A is fast but rough, Option B is slow but robust — I'd take B because this is load-bearing; agree?" Options without a recommendation just push the thinking back onto the user; the bet is what makes it a grilling.
- **Challenge assumptions.** "You mentioned React — is that because the team knows it, or is there a hard technical requirement? What if we used something lighter?"
- **Surface risks.** "Given this timeline, if we discover mid-way that the data format is incompatible, rolling back would be expensive — should we do a quick spike first?"

### 4. Produce an alignment document

After questioning, output a concise alignment document:

```
# Alignment Document: [Task Name]

## Goal
[One sentence: what are we trying to achieve]

## Success Criteria
- [ ] Concrete, verifiable condition 1
- [ ] Concrete, verifiable condition 2

## Constraints
- Time: [deadline or time budget]
- Tech: [language / framework / platform restrictions]
- Scope: [explicitly out of scope]

## Priorities
1. [Highest priority]
2. [Second priority]

## Decisions Made
- [Decision]: [chosen path, with brief rationale]
- [Decision]: [chosen path, with brief rationale]

## Risks & Assumptions
- Risk: [what could go wrong]
- Assumption: [premises we're treating as true]

## Context
[Background, previous attempts, relevant links]
```

For a heavyweight alignment that settles a **load-bearing design decision** (chose X over Y, and the reasoning), also record it per repo discipline in `decisions/YYYY-MM-DD-<topic>.md` — one curated entry per decision. The alignment document is the working artifact; `decisions/` is the durable record of *why*. Follow the repo's doc-maintenance discipline; don't reach for heavier domain-modeling machinery.

### 5. Confirm, then proceed

Send the alignment document to the user for confirmation. **Do not enter the Plan phase, write code, or take any action until the user explicitly confirms you have reached a shared understanding.** "Looks roughly right" is not confirmation — press until the user signs off on the document as written, or corrects it. If the user corrects anything, update the document and reconfirm. Your own confidence that you understand is never a substitute for their explicit go-ahead.

**The alignment document is the input to the Plan phase — or straight to execution.** After confirmation, if the task is small or serial, start executing directly; only very large or parallel tasks need a Plan phase (see `plan/SKILL.md`).

**When the aligned work will run autonomously** — a batch you execute unsupervised rather than step by step — the confirmation doubles as the single authorization for the whole run. Fold the alignment into one `ava.ui.notify(require_response=True)` stating the scope, the task list you will open (the `ava.tasks.create()` items), and the budget ceiling in dollars; the user's reply is the grant, recorded verbatim in the driving task's `description`. This one approval is what buys high Autonomy — after it you run without further check-ins (`ava_fleet`'s Autonomy dial and its boundaries).

## The Evaluation Connection

Alignment is where **the evaluation standard is born**: "what counts as good" is defined here, and every later check measures against it.

- The **Success Criteria** section is the acceptance test. Work & Evaluate verifies against it item by item; a criterion that can't be checked ("high quality", "fast") is a criterion that was never actually aligned — push until it's concrete and verifiable.
- **Priorities** are evaluation weights: when two criteria conflict mid-execution, the priority order is the tiebreaker. If the user says "when time runs short, cut X first," that *is* an evaluation rule.
- **Risks & Assumptions** are the evaluation watchlist: each listed risk is a thing to actively check for during execution, not a static note.
- Mid-execution, if evaluation reveals the *goal itself* was wrong (not just a step), that's an alignment failure — return here, re-question the affected branch, and update the document before continuing. Evaluation doesn't only judge execution; it judges alignment too.

## Standalone Use

Align can be used independently — any time you feel the task isn't clear enough, align before building. You can do alignment without entering the later phases. It works for non-coding decisions too — architecture choices, tool selection, process changes, even personal decisions.

## Don't

- Don't accept "just start and we'll see" — question until there are clear success criteria
- Don't dump 20 questions in one round — keep each round compact (≤5-8), but keep running rounds until the frontier is empty. A tight round cap is not a total-question cap; no branch gets silently assumed to stay under some number.
- Don't ask a question without your recommended answer attached — if you can't recommend one, you haven't explored enough to be asking it yet
- Don't stop questioning when the user says "roughly okay is fine" — ask "what's the worst acceptable outcome?"
- Don't ask the user for facts you can look up — explore the environment first, and spin up a sub-agent for facts that surface mid-grilling
- Don't skip branches in the decision tree — each unresolved branch is a future rework waiting to happen
- Don't align on top of an uncalibrated model — if the user's picture of the subject is wrong, calibrate that slice first
