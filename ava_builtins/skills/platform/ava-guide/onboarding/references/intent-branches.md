# Onboarding intent branches

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Intent branches

The answer to "what do you want this cluster for?" falls into one of four
branches. Follow the branch; if the user is undecided, walk branch B until
they land somewhere.

### A. A concrete goal ("track my health", "watch this company")

1. Restate the goal in one sentence and ask "is this the target?" — lock
   the target before proposing anything.
2. Ask what done looks like and the constraints (cadence, budget, what not
   to touch).
3. Record goal + constraints as `type/project`.
4. Decompose into tasks. If it is large, load `ava.skills.ava_workflow`
   (calibrate → align → plan) and `ava.skills.ava_fleet` for
   parallelization. For the first task, one small real step beats a grand
   plan: create the task (`ava.tasks.create`) or spawn the first worker,
   and tell the user what is running.

### B. "What can you do?" / vague

1. Do not recite the skill catalog. Show one capability on the user's own
   material — "paste a link and I will summarize it", "give me a topic and
   I will research it". One live demo beats a tour.
2. Ask what they spend their time on; route the demo toward that.
3. End by proposing the first small task drawn from what they mentioned,
   and start it.

### C. Ongoing services ("keep an eye on X", "manage my Y")

1. For each domain they name, propose one dedicated role agent — long-
   running, owns that domain, reports on a cadence.
2. Agree each role's boundary before spawning: what it owns, what it may
   never touch.
3. Record each role as `type/role` with its boundary.
4. For time-triggered work, load `ava.skills.ava_guide.schedules` and
   create the schedule; spawn the first role agent with a self-contained
   prompt naming the domain and the cadence.

### D. Evaluating Ava itself

1. Explain in one paragraph: agents + one tool (`execute_code`) + skills +
   memory; you are one agent in a fleet, peers get spawned per task.
2. State honest limits: you can be wrong; irreversible and outward-facing
   actions always ask first; skills are instructions you read, not
   guarantees.
3. Offer a contained trial: one small task, a clear success criterion, no
   standing commitments. If the trial succeeds, treat it as branch A.
