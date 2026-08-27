# User-Dimension Maintenance — full heuristic

This is the reference for the `User-Dimension Maintenance` section of
`skills/SKILL.md` (loaded flat as `ava.skills.ava_memory`). Read it when you
write memory about the user, when you maintain a user-dimension note, or when
consolidation surfaces overlapping user-preference notes.

## The heuristic

The pool's discipline leans toward the **agent dimension** — what an agent
did, how it did it, where its key facts live. The user has a dimension too,
and it deserves the same continuous care. In one sentence: **maintain the
user side of memory the way you maintain your own, so the user never has to
say the same thing twice.**

When writing memory, deliberately capture the user dimension:

- **Repeatedly expressed preferences** — anything the user has said more than
  once ("speak Chinese", "no nin-politeness", review style, communication channel). A repeat
  is a signal the preference is not yet durably captured; capture it so the
  repetition can stop.
- **Recurring habits** — patterns the user reliably shows in how they work
  (brainstorm → fan-out → converge; align direction before executing; Keep It
  Simple as the tie-breaker).
- **Corrected behaviors** — every time the user pushes back on how an agent
  behaved, the correction is a memory-write trigger, not a one-off apology:
  write the corrected preference as a rule with the why, in the same turn the
  correction happens.
- **What the user values / does not value** — the meta-layer: what the user
  weighs when judging work (concept simplicity over literal simplicity, no
  dev-time estimates, diff-tree rigor) and what they explicitly don't care
  about (ceremony, emoji, optimistic updates).

The design stance: **if memory is good enough, explicit user modeling is
unnecessary.** No user-profile subsystem, no schema, no separate store — the
user dimension lives as natural-language notes in the same pool, maintained
the same way everything else is: write → validate → consolidate. The success
test is behavioral: the user stops repeating himself.

## User-dimension notes are standing objects, not one-off files

A deployment's pool typically carries a set of user-dimension notes. They are
**live, continuously-maintained objects** — the canonical record of the user
dimension — not historical records. Example notes a deployment may keep:

| Note | Holds | Maintenance rule |
|------|-------|------------------|
| `user-profile.md` | who the user is — identity, health, career, contact, base preferences | update in place when facts change (job, address, appointments); never append episodes |
| `user-preference-rules.md` | pointer to the full preference rules (wherever the deployment keeps long-form rules) | keep the pointer current; a preference confirmed more than once is folded into the rules file, not into new pool notes |
| `collaboration-preferences.md` | how the user wants to work with agents | the first place a correction is folded in; keep the rule + why shape |
| `user-core-principle.md` | the meta-principle (Keep It Simple) | extend only when the user restates or refines the meta-layer; keep it short |

## Maintenance split

- **Every agent**: when you learn something about the user, update the
  matching note in place (append your id to `authors` per the editing rules)
  or write a small new note. A correction lands in the same turn it happens.
- **Memory Arbiter**: owns coherence during consolidation — merge overlapping
  user-preference notes written by different agents into the canonical set
  above, and promote a preference that has been confirmed more than once from
  a transient note into the standing notes. A duplicate written by two agents
  is a signal the canonical note is not discoverable enough; fix the canonical
  note, don't just delete the newcomer.

## Per-agent memory

The shared pool holds the cluster-wide canonical user facts; your own
`memory/` (per-agent memory) holds the domain-specific ones — how the user
wants *this* kind of work done. Same rule as above: when the user corrects
you, the correction lands in your personal memory in the same turn, before
the turn is considered finished.
