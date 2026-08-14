# Release-directory atomic code swap — bare-metal "immutable artifact"

> Status (2026-06-04): **deferred, not started.** Research/alignment note only.
> Came out of a "are we reinventing Kubernetes?" discussion. Decision: document
> the mechanism + options, do not build yet — the footgun this targets is rare and
> already half-mitigated. Build when it bites often enough to pay for the disk +
> release-GC layer. Related: [`commit-pinned-cluster.md`](commit-pinned-cluster.md)
> and the recovery flow in `cli/commands/_update_recover.py`.

## Why this came up

Ava runs on **pet machines** (laptops, small servers, cloud VPS), so the
container answer to safe upgrades — ship an immutable image, replace the whole
machine — does not apply: you cannot rebuild a laptop per deploy. That pushed us to
**in-place** `git checkout --force` + `uv sync`. The question is whether the
*atomic + rollback-able* property of immutable artifacts can be had on bare metal
without containers. It can: **release directory + `current` symlink** (the
Capistrano pattern), pure filesystem.

## What is "dirty" about the current in-place upgrade

The mutation point is `cli/commands/update.py` (`_run_gateway_local_update`):
`git checkout --force -B main <sha>` + `uv sync` happen **in the same directory the
running processes import from**.

- `cli/commands/_repo.py:_repo_root()` = `Path(__file__).resolve().parents[2]` — the
  code root is "wherever the running `.py` lives." (This is *favorable* for releases;
  see below.)
- The lazy-import drift hazard is already known: `ava start` is run as a **fresh
  subprocess** specifically to avoid mixing pre-pull imported modules with
  post-pull lazy imports.
- **Residual footgun = the orchestrator itself.** `ava update --local` runs in a
  detached session on the *old* interpreter while it rewrites its own directory
  underneath it. Any lazy import / subprocess call it makes after the checkout sees
  new code. This is the "self-upgrade across a commit that deletes a CLI subcommand
  blows up" class.

## What release-swap buys — and what it does not

Mechanism: one `~/ava-releases/<sha>/` per commit (git worktree, shared `.git`
objects), each with its own `.venv`; a `current` symlink points at the active
release; **processes cd into the release's realpath (not through the symlink)** so a
running process's files never change underneath it. Upgrade = build new release →
graceful-stop + relaunch each service pointed at it. Rollback = flip the symlink +
bounce.

**Buys (real):**
- Eliminates the entire "mutate code under a running process" class — orchestrator
  self-drift, lazy-import mismatch, dirty-tree recovery, partial `uv sync` seen by
  live code.
- Nearly free to make processes release-pinned, because `_repo_root()` is already
  `__file__`-relative — watchdog/restarter respawns use their own `repo` root, so a
  respawn cannot silently jump versions.
- Code rollback becomes "flip a symlink" instead of "git reset + hope."

**Does NOT buy (be honest):** the quiesce / Phase-A stop-the-world / fan-out
machinery exists for **schema**, not code. The shared central DB + forward-only
migrations mean "no old-code agent writes the new schema" is untouched by a symlink.
Release-swap fixes **single-host code consistency**; it does nothing for
**cluster-level schema coordination** — which is the actual source of the
orchestration's complexity.

## Cost, and a codebase-specific synergy

- **Disk:** `uv` venvs hardlink from `~/.cache/uv` (cheap per release); git objects
  shared via worktree. The real cost is `frontend/node_modules` + `.next` per
  release (hundreds of MB; npm does not share like uv).
- **Synergy:** `shared.repo_change`'s frontend/backend change classification (already
  used to skip the npm build) maps directly onto "does this release need a fresh
  `node_modules`/`.next` or can it symlink the previous release's?" Backend-only
  changes (the common case) reuse the prior frontend build.
- **New layer:** release creation + atomic symlink flip (via `rename`) + keep-last-K
  GC. `~/.ava/` (pidfiles, machine state, memory pool) already lives outside the repo,
  so it is release-independent — no change.

## Options (for when this is picked up)

1. **80/20 — orchestrator snapshot only.** Don't convert the system to release
   dirs. Just have `ava update` `git worktree add` a throwaway worktree at the *old*
   HEAD and run the detached `ava update --local` from there, so the orchestrator's
   files never change under it; the main repo still checks out in place. Touches only
   where `spawn_rollout` launches the session. Kills the one footgun that has actually
   bitten, an order of magnitude cheaper than the full system.
2. **Full release-directory system.** release dirs + `current` symlink + release-pinned
   launch/respawn + classification-driven `node_modules` GC. Atomic code upgrade and
   rollback; schema coordination unchanged. Moderate effort — touches the launch,
   respawn, and update paths and adds a GC layer.

## Out of scope here

The remaining hard problem — making cluster-wide schema migrations not require a
stop-the-world quiesce (expand/contract / backward-compatible migrations) — is the
real orchestration-complexity driver and is independent of code-swap. If pursued,
it belongs in its own note.
