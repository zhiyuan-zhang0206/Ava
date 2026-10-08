# Dynamic Workflow Execution

Read this procedure when script orchestration is selected. Run examples from
the skill directory unless another working directory is specified.

## Explore → Dispatch → Collect → Reduce

### 1. Define independent units

Choose each unit's input, outcome, acceptance evidence, and delivery location.
Batch cheap units together to avoid paying for one peer per tiny item. Respect
agreed scope and exclusions. A peer brief must contain the relevant context and
explicit result contract; each peer can select its own applicable skills.

When USD observation is needed, use `ava-being-a-long-running-agent`'s existing
usage script with the chosen agent IDs, birth lineage, and time window. Include
all participating peers and probes in the agreed budget. Record the observation
scope and cutoff; missing or in-flight costs are not zero. Budget reminders ask
agents to make decisions; they do not kill peers or grant more spending.

### 2. Dispatch and save receipts

Read [minimal_dispatch.py](minimal_dispatch.py) for a small starting
example. Its single-writer run stores dispatch intent before spawning and the
returned peer ID immediately afterwards. It preserves completed results and
never automatically redispatches a known or ambiguous assignment. Adapt its
result envelope to the domain; do not treat it as a production workflow runtime.

```python
# Execute the reference's function definitions, then choose a run directory and
# self-contained task briefs. Start with one unit; add a useful batch after it works.
from pathlib import Path

report = run(Path(ava.cwd.get()) / "handoff", [
    {"id": "unit-1", "prompt": "Analyze this input ...; return evidence ..."},
])
print(report)
```

A spawn receipt means accepted, not verified execution or completion. Save IDs
and inspect progress only when needed. If dispatch fails, propagate the failure
and reconcile what was accepted; do not proceed into a blind result wait.

A local file write and remote spawn are not one transaction. If interrupted
between them, an intent may lack its peer receipt. Reconcile actual peers and
artifacts before retrying; uncertainty is not permission to spawn again. Never
claim arbitrary-crash exactly-once execution from a JSON state file alone.

### 3. Collect at useful checkpoints

Routine results can be atomic files, a shared record, or another explicit
handoff. A file's existence is a readiness hint, not proof of completion:
validate its task identity, input version, schema, and required evidence before
accepting it. Write partial work separately and identify it as partial.

Preserve matching results on re-entry. Use a distinct run or input-version
location for changed work; do not delete all previous results to begin a new
wave. Retain known peer IDs, dispositions, and the next decision. Recovery does
not mean automatically retrying every missing file.

For a long wait, choose one checkpoint over the results that gate the next
useful action. [gather_files.py](gather_files.py) can send a single
wake when all, K-of-N, or named results exist. It checks existence only; the
responsible peer must validate contents after waking. Use a versioned directory,
save the watcher session ID, and avoid arming duplicates on re-entry. Completed
files remain observable if the watcher starts after peers, so there is no need
to arm a watcher before an uncertain dispatch just to avoid a missed event.

A short bounded script wait can suit a probe. Check successful dispatch first;
a timeout must surface missing units and known IDs, not a completion claim.
Routine per-peer completion messages can cause unnecessary model turns; choose
an aggregated checkpoint when useful. Blockers, budget decisions, and handoffs
must still reach whoever needs to act. Do not impose silence on those messages.

### 4. Verify and reduce

Check input coverage, duplicate units, result validity, and domain evidence.
Synthesize only validated results; clearly identify missing or partial work.
For software, exercise behavior rather than accepting a peer's green-test claim.
Choose independent review when consequences or uncertainty justify it.

Return the requested artifact. A summary does not require a web page, a new
service, a report framework, or extra publication. Preserve enough evidence and
working state to continue if needed.

## Budget reminders and deliberate pauses

If a budget reminder or other evidence calls for a pause, record that decision
before further dispatch. Preserve results and unfinished units, peer IDs,
checkpoint IDs, accounting scope, and a concrete resume condition. Ask active
peers to finish a safe unit and hand off; a cost threshold is not a hard kill.

On checkpoint, script re-entry, or restart, read that state first. A late result
does not clear a pause or authorize the next wave. Resume under the recorded
condition using existing peers and artifacts. Stop unneeded watchers by their
recorded session IDs. Peers can remain idle for follow-up or end themselves;
termination is a deliberate lifecycle choice, not this skill's universal
completion protocol. Agent persistence alone does not make a script resumable.

## References on Demand

| Reference | Use |
|---|---|
| `references/minimal_dispatch.py` | First probe and receipt-preserving dispatch; ambiguity stops for reconciliation |
| `references/gather_files.py` | File-existence checkpoint; validate results after waking |
| `references/orchestrator_template.py` | One-shot fan-out/checkpoint illustration; not a resumable template |
| `references/deep_research_orchestrator.py` | Larger research illustration, seven waves |
| `references/codebase_sweep_orchestrator.py` | Larger code-sweep illustration, seven waves |
| `scripts/deep_research_lite.py`, `scripts/codebase_sweep_lite.py` | Multi-wave demos; inspect state and failure assumptions before adapting |

Read only the reference needed for the current gap. Larger demos illustrate
composition; they are not default setup for small batches. Generate orchestration
freely as the task demands, keeping authority, USD budgets, and evidence explicit.
