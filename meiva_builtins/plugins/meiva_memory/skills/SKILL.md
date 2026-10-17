---
name: meiva-memory
description: "Maintains and queries Meiva's shared memory. Use when finding shared facts, preserving user preferences, or performing assigned memory maintenance."
---

# Meiva shared memory

Use the shared pool for facts and preferences other agents need; keep detailed
working state in the current agent's workspace. This skill does not assign an
arbiter or steward role. Follow the role and scope supplied by the current task.

## Select the needed guide

- For a query, use `meiva.memory.search("<query>")`; each match carries a relevance
  `score` and the `chunk` that matched, so weigh a weak score before opening the
  note, then read the matching note and inspect its ownership. Another agent's role is context,
  not your identity. Do not start maintenance merely to answer a query.
- To write or update a note, search first, then write it with `meiva.files.write`
  under `meiva.memory.PATH` (shared) or `meiva.memory.PERSONAL_PATH` (your own),
  following the frontmatter shown by `help(meiva.memory.PATH)`. After the call a
  reminder may name a rule the note breaks or an existing note that looks like a
  duplicate; fix the former and merge into the latter. There is no writer
  function, and the index files are rebuilt at consolidation, not by you.
- When recording user preferences or corrections, read
  [user-dimension maintenance](reference/user-dimension.md). Search first,
  update the existing standing note, and keep durable shared facts in the pool.
- For consolidation, read [consolidation](consolidation/SKILL.md), then only
  the single-box, arbiter, or steward procedure matching this deployment and
  the explicitly assigned role. Stewards publish local work; arbiters merge
  across machines and verify search refresh.
- Only when acting as the assigned Memory Arbiter, read
  [the arbiter role guide](reference/arbiter-role.md) for health checks,
  workspace-note curation, schedules, and collaboration responsibilities.

Validate changed notes with the pool's own validator. Keep its metadata,
ownership, size, and index rules; large source material belongs in the configured
Vault with a searchable pointer. A successful commit or merge does not prove
the gateway search index was refreshed. Preserve the user's existing authority
for writes, messages, schedules, and maintenance.
