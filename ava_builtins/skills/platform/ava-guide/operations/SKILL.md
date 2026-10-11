---
name: operations
description: "Diagnoses Ava production alerts and failures. Use for incident triage, health investigations, recovery, or rollout verification."
---

# Operating the Ava cluster — production diagnosis playbook

This skill is the cluster operator's day-to-day companion: how to review the
alert stream, how to diagnose the recurring failure classes, and how to
respond. It is **methodology** (how to find and judge), not a fix index —
for symptom → fix recovery of a broken cluster see `docs/conventions/runbook.md`
(a stranded maintenance hold: `docs/conventions/operations/graceful-maintenance.md`); for the CLI verbs see
`ava-guide/ops`; for rollout safety see `ava-self-development`.

## Role

- The cluster operator owns: disk / worktree / runtime health, opening and
  closing clusters, node recovery (detect + resurrect), and **rollouts are
  executed only by the operator** (no agent self-updates).
- Node anomaly detection and resurrection are centralized on the operator —
  any agent finding an anomaly reports to the operator, not to the peer that
  discovered it.
- Alert review runs on a schedule (watcher, e.g. every 2h window): triage the
  window's new alerts, classify, and record the disposition. Every alert must
  end up in one of: **known/closed** (already handled — with the incident
  reference), **transient** (self-resolved, no action), or **action needed**
  (investigate now).

## Cross-agent context and workspaces

Start with `ava agents ls`: its ID, status, machine, and label columns map the
sibling agent to its execution context. For an agent owned by this host, its
workspace is `<cluster-home>/workspaces/<id>`. The cluster home is the
`AVA_HOME` your own environment carries (the host keeps no list of clusters);
the default production home is `~/.ava`. A remote agent's workspace lives on its owning machine — never
infer it by joining the ID to the gateway's home.

Inside a workspace, read `memory/MEMORY.md` first and follow only the entries it
links when reconstructing durable context. After the sibling CLI PR lands, use
`ava memory search <query>` to find shared cluster memory rather than
guessing from one agent's files. These are read-only discovery steps; rollout
and destructive-action approval boundaries remain the ones in **Response
discipline** below.

## Alert review workflow

1. **Classify by attribution first** — pull the alert list for the window,
   group by service/pattern, and match each group against known incidents
   (this skill's playbooks, memory notes, recent state files). A cluster of
   alerts that maps to an already-handled incident is *known/closed* — record
   it and move on; do not re-investigate.
2. **Transient vs persistent** — a one-shot warning (a dropped SSE event, a
   single slow DB acquire, a statement timeout) is transient: note it, no
   action. A *repeating* pattern (same service, same error, several times) or
   a *state* that outlives the window (a breaker tripped, a host paused) is
   persistent: investigate.
3. **Trace the root-cause chain, not the symptom** — repeated respawns of the
   same service are usually a *cause* elsewhere (a port held by another unit,
   a session backend mismatch, a dead parent). Ask "what keeps killing it?"
   before fixing the respawn itself. Check the machine the event names — a
   query that does not filter by machine can misattribute a win/wsl event to
   the wrong host (seen repeatedly).
4. **Verify the fix, then close** — after an intervention, confirm the alert
   stops (zero new occurrences past the intervention timestamp) before
   marking closed. Record the incident (what, why, fix, prevention) in the
   operator's memory or state file so the next review recognizes it.

## Diagnosis playbooks

Read only the matching [diagnosis playbook](references/diagnosis.md) for disk,
memory, connectivity, process, schedule, or delivery symptoms.

## Post-rollout verification checklist (accumulated from real rollouts)

After an authorized rollout, read [rollout verification](references/rollout-verification.md)
and check the actual running generation, service roster, and representative
agent progress. A merge or skipped check is not proof of runtime health.

## Response discipline

1. **Stop the bleed within authority.** First attribute the machine, unit,
   exact service/session, and actor. Report the smallest supported official
   lifecycle mitigation and its blast radius. Do not use raw signals, direct
   database state writes, production source edits, or broad restart/update as
   shortcuts. A temporary recovery is not a persistent fix; label it and send
   the defect through PR, review and CI. Explicit user constraints override
   generic incident playbooks.
2. **User approval boundaries.** Rollouts/cluster updates need user approval
   (a standing ruling; offline-tolerance carve-outs are granted separately).
   Irreversible actions (deleting data, force-pushing, killing production
   processes) also need user sign-off — one approval is scoped to one action.
   Emergency *restoration* (stopping a crash loop, freeing a stuck port) is
   within the operator's authority; *changes* (new pins, new config) are not.
3. **Verify, then record.** Every incident ends with: the alert stream quiet,
   the fix deployed or queued, and a memory/state note that makes the next
   alert review recognize the pattern (this is how "unknown alert" becomes
   "known/closed").
4. **Learned constraints are memory, not lore.** When a diagnosis reveals a
   durable environment fact (a port a system service grabs, a backend
   mismatch class, a machine's offline pattern), write it to the operator's
   memory so a fresh process inherits it.
