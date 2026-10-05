# Skills: single converged load dir (`~/.ava/skills/`), installed ≠ enabled

## Decision

Collapse the skill scanner's five mount points (repo `skills/`, `~/.ava/skills/`
overlay, provider roots, repo `plugins/*/skills/`, `~/.ava/plugins/*/skills/`)
to **one on-disk load dir** — `$AVA_HOME/skills/` — plus runtime provider roots.
A converge step (`cli/commands/_converge_skills.py`, run by `ava start` /
`ava update` / `ava converge` and inline after `ava plugins install`/`upgrade`)
syncs repo and plugin skills into it, tracked by the install registry.

Two orthogonal dimensions replace the implicit install==enabled coupling:
**installed** (the dir exists on disk) and **enabled** (the registry entry says
the scanner loads it). `ava skill enable/disable` flips the toggle without
touching disk; `ava skill register` adopts a hand-copied dir; an untracked dir
never loads.

## Why

- Agents could not answer "where does this skill live on disk?" — five mounts
  meant the answer depended on provenance. One dir makes `ava.help()`'s path
  line uniformly actionable (edit, read sibling files).
- The old model coupled repo skills to the checkout and gated only the overlay,
  so "disable a repo skill on this machine" had no mechanism at all.

## Key choices

- **Converged copies are derived state, hash-guarded.** The registry records
  `content_hash` (tree hash of what converge last wrote). Untouched copies
  follow their source — overwrite on change, delete when the source vanishes;
  a user-edited copy is never overwritten and never auto-removed (warn only).
  This reconciles "auto-clean derived state" with "never destroy user content".
- **Registry fields are additive, not the design-doc rename.** The design
  called for `source: "repo"|"plugin"|"user"`, but `InstalledPackage.source`
  already carried the install git URL (and `ava plugins upgrade` depends on
  it); renaming would need a migration and the fail-fast registry load would
  brick `ava start` on any not-yet-migrated host. Added `origin` /
  `origin_path` / `content_hash` / `installed_at` / `updated_at` instead.
- **An installed plugin's skills gate on the plugin's own registry entry** —
  no duplicate `type="skill"` row for `~/.ava/skills/<plugin>/`. The registry
  stays name-keyed with unique names.
- **User package wins name conflicts.** A user-installed package squatting a
  repo/plugin skill's name shadows the source (warn), because overwriting
  would destroy user content — the one deliberate deviation from the design's
  "repo wins on the same path".
- **Untracked dirs (e.g. legacy `auto-review`, `wechat-ocr`) are not
  auto-registered** — converge warns and points at `ava skill register`;
  preserves the pre-change behavior (they did not load before either).

## Rejected

- Loading plugin skills from their source trees at scan time (status quo):
  keeps the "where is it" ambiguity, and external plugin skills bypass the
  enabled gate entirely.
- Auto-registering every dir found in the load dir: silently promotes strays,
  the exact shadowing failure the registry gate exists to prevent.
