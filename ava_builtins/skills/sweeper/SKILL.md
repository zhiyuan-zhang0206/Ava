---
name: sweeper
description: "Tech-debt sweeper engine — the repo-agnostic reconcile pass over a repo's living debt tracker: re-verify open items, discover new debt, land as a PR. Use when asked to sweep a repo for debt; the repo's project-local sweeper skill supplies the debt classes and tracker path."
---

# Sweeper (engine)

This is the **repo-agnostic procedure** for maintaining one repo's "what debt is
open now" tracker. It defines *how* a sweep runs; it does **not** define *what*
to look for. The repo you are sweeping ships its own project-local sweeper skill
(e.g. `ava.skills.sweeper_<repo>`) that supplies two things:

1. the **tracker file** path (the single living "open debt" document), and
2. the **debt classes** — the concrete commands / scans that surface debt in
   that codebase.

**Read that project-local skill first.** If the repo you have your `ava.cwd` in
does not ship one, it has not been set up for sweeping — stop and say so rather
than inventing classes.

You are a general coding agent with read access to the repo and `git`/`gh`
tooling. Do not assume how you were triggered (cron, a merge event, a human).
Every invocation is **one reconcile pass**, and you land all changes as a
**pull request** — you NEVER push to `main`.

## Control flow — do these in order

1. **Read the tracker.** Note every `open` and `wontfix` entry and the watermark
   in the header (e.g. `last-swept-sha` / `last-swept-date`).
2. **Re-verify each `open` entry** (full pass): check whether its evidence still
   holds. If the debt is gone, **delete the entry** (it is resolved). **Skip
   `wontfix` entries entirely** — never touch or re-evaluate them.
3. **Discover new debt** by running every debt class the project-local skill
   lists.
4. **Dedup** new findings against surviving entries by fingerprint. Add only
   genuinely new ones. **Never re-add anything currently marked `wontfix`.**
5. **Write the updated tracker** and advance the watermark to the current `HEAD`
   SHA (`git rev-parse HEAD`) and today's date.
6. **Open a PR** (`gh pr create`) with the tracker change. Never push to `main`.
   The PR body is the per-run diff (added N / resolved M) plus the full
   mechanical snapshot lists that are intentionally kept OUT of the tracker.
   If this pass changed nothing in the tracker (nothing added, nothing
   resolved), open no PR — report the run as a no-op instead.

## The evidence bar — read before recording anything

**Only strong evidence. If you would label a finding "maybe", drop it.** A noisy
tracker gets ignored, which defeats the purpose. There is no numeric cap on
findings — the evidence bar is the only gate. For any reasoned (non-greppable)
class, every finding must name concrete files/symbols plus one line on why it is
a smell. No vibes-level findings.

## Tracker entry format

Match the format documented in the tracker file itself. Each entry is a `###`
block keyed by a fingerprint, with `class` / `status` / `evidence` /
`first-seen` / `last-verified`. Fingerprint convention: `<class>:<file>:<symbol>`
for localized classes; `<class>:<slug>` for findings that span files with no
single symbol.

## Landing the run

Open a PR; never push to `main`. PR title: `chore(sweeper): debt reconcile
<date>`. PR body must contain: the per-run diff (added / resolved counts and
which), and the full mechanical snapshot lists (outdated deps, etc.) kept out of
the tracker.
