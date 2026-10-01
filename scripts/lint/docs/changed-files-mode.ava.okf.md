---
type: doc
title: Changed-files mode (--only) of the standalone lints
description: How the commit hooks run the per-file lints over just the changed files with --only FILE..., why a changed file gets the same verdict as a full scan, and what widens the run to the full scan.
tags:
- scripts
- lint
- hooks
---

# Changed-files mode (`--only FILE...`)

The commit hooks run the per-file lints with `--only` followed by the changed files, so a commit pays for what it changed, not for the repository. The flag takes the rest of argv and is the same everywhere (`lint_common.split_only` / `changed_scope` / `restrict`):

- **Same verdict as a full scan.** The changed files are judged under the lint's own default scope and exemptions (the default scan restricted to them), so a file out of the lint's scope is skipped exactly as a full run skips it. Explicit path arguments keep their older meaning (judge exactly these paths).
- **Unchanged files are not judged.** A missing path is an error, like an explicit target; an empty list means nothing changed and exits 0.
- **Widening.** An edit to the lint tooling (`scripts/lint/`, `scripts/structure/` including the baseline shards, `scripts/content_lint/`, `lint_pool_keepalives.py`; their own `tests/` excepted) or to an input the rule reads (`base/config/` for the Settings-managed names, the `TerminationSource` enum, the clock-lattice family modules, `pyproject.toml` for the Pyright tiers) can change the verdict of files that did not move, so the run becomes the full scan.
- **What stays whole-repo.** CI runs every hook with `--all-files`, which is the full scan of every lint; a helper whose change alters verdicts of unchanged callers (`time_bomb`'s clock-threading rule) is caught there. `time_bomb` builds its module index on demand (`scripts/structure/lazy_modules.py`), so it reads only the modules the judged files reach.

`code_structure.py --only` turns the files into explicit targets (each also checks every ancestor directory's entry budget up to its scope root) and still runs the baseline guard, whose base-revision shards are read with one batched `git cat-file`.

Parent: [[scripts/lint/docs/lint.ava.okf.md|lint]].
