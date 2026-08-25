---
name: workspace-cleanup
description: Dispose of dead agents' workspaces when disk pressure or monthly inspection demands it — zero-loss first, dead-agent criteria, disposal criteria, tombstone, ledger.
---

# Ava Ops — Workspace Cold-Data Disposal

## Zero-loss principle

The core principle (user ruling 2026-08-25): **preserve all information — no
data loss, ever.** The memory system is not mature enough yet; anything
deleted may be unrecoverable. Disposal is classification-and-preservation,
not deletion:

- **Duplicate PDFs** — organize shared PDFs and store them in one place on
  Google Drive.
- **Public repo clones** — convert to shallow clones, or delete the local
  copy keeping only the URL.
- **Hardware fallback** — when disk is still insufficient after the above,
  buy a larger-storage Mac mini (pre-approved by the user) instead of
  over-optimizing deletions.

Periodically run a dynamic workflow to sweep workspaces: classify how much
content belongs in the Vault and how much belongs in memory (apply the shared
memory-vault rule).

## Trigger & Ownership

Run disposal when the disk watermark exceeds 85% or during the monthly
inspection. Hook it into the existing schedule system: fold it into the routine
sweep or use a separate cron. The default executor is #312 or #1818.

Determine that an agent is dead only when both conditions hold:

- `last_active` is more than 30 days ago.
- All tasks owned by the agent are `closed`.

Do not resurrect a dead agent to clean up its own workspace. Resurrection causes
cache misses and wastes tokens. First call
`ava.agents.get_ancestors(agent_id)`. If a live ancestor exists, hand over the
workspace with a **7-day deadline**. If no action occurs within that deadline,
the executor disposes of the workspace as usual, writes the tombstone, and
notifies the ancestor. This prevents a handover from becoming permanent
shelving.

If no live ancestor exists, #312 or #1818 disposes of the workspace.

Removing a workspace directory does not affect agent resurrection because
sessions and checkpoints live in Postgres. Do not treat directory removal as
breaking the agent.

## Disposal criteria

Apply these quantified criteria without on-the-spot judgment:

- Delete regenerable content such as `.venv`, `node_modules`, caches, and logs
  directly. Do not archive it.
- Archive everything else to Drive under `My Drive/ava-cold-*/` as an
  online-only placeholder.

## Tombstone & ledger

- Write the tombstone at the parent level:
  `~/.ava/workspaces/<id>.TOMBSTONE.md`. Include the time, reason, destination,
  and recovery procedure. Never put it inside the directory being moved; it
  would disappear with that directory.
- Keep the ledger at a fixed location: one shared-memory index entry and one
  record per disposed directory. Each record carries the sha256 manifest path.
- Use the ledger as the **archive exemption list**. Register archived
  directories as exempt and exclude them from whole-tree backup, indexer, and
  disk scans so one full scan cannot rehydrate the whole disk.
- Store the sha256 manifest twice: locally and at a non-placeholder Drive path.

## Red lines

- Never touch the workspace of a resident role or an agent with an in-flight
  task.
- If the workspace contains user deliverables, including reports or links the
  user has used, notify #405 before disposing of it.
