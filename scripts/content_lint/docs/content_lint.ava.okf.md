---
type: doc
title: scripts/content_lint/ — Document and Content Lints
description: The lint_*.py / check_*.py document, OKF, skill, and migration-format guards in scripts/content_lint/ — what each enforces and where it runs. Code/AST convention lints split out to scripts/lint/.
tags:
- scripts
- lint
---

# scripts/content_lint/ — Document and Content Lints

Document, content, and schema/format tripwires, mostly invoked by
`.pre-commit-config.yaml` and CI. Code/AST/Python-convention lints are a
separate group, including the shared CLI-contract note for explicit-target
lints: [[scripts/lint/docs/lint.ava.okf.md]].

## The linters

- `lint_ava_okf.py` — OKF format validation (frontmatter / size / wikilink); reuses `../../codegen/build_okf_data.py`'s wikilink resolver so linting a subtree does not false-positive on cross-domain links. `files:`-filtered at pre-commit; `lint-prepush-artifact-freshness` re-runs it (`--all-files`) at pre-push when the branch only deleted `.ava.okf.md` inputs, since a `files:`-filtered hook never sees a purely deleted one on any range.
- `lint_no_tailnet.py` — bans 100.64.0.0/10 host literals; allows CIDR, frozen `decisions/`, and `# tailnet-ip-ok:` test lines. Pre-commit + `repo-language` CI (2026-08-03/04 Gateway-URL, 2026-08-20 public-repo rulings).
- `lint_no_cjk.py` — repo-wide whole-repo scan (git ls-files) banning raw CJK outside the documented i18n-locale exemption (user ruling 2026-08-27).
- `../../structure/lint_common.py` — tracked files, target resolution, UTF-8 reads, and `file:line` output shared by `lint_no_cjk.py` and `lint_no_tailnet.py`.
- `lint_migrations.py` — timestamp-id + applied-set scheme checks: filename format, unique names, no merged migration modified/deleted/renamed (against the `origin/main` merge-base or `--base`), `db/schema.sql` baseline/folded-migration stamping, and a later-drop plan for every `*_backfill_*` snapshot table; **no expand-contract check** (that's a documentation discipline, not lint).
- `lint_audit_record.py` — every `category=audit` emit site must record the event to Postgres (`record_audit` / `record_audit_standalone`, `base/telemetry/audit_events.py`) before emitting it; sites that predate the primitives are an exact `path::function -> count` map in the script (growth and shrinkage both fail).
- `check_cross_branch_migrations.py` — tripwire: fails if the single-branch premise this repo's migrations rest on breaks.
- `lint_core_content_manifests.py` — core-content manifest ranges + `requires_commit` vs. the derived host version; the range a derived-version switch would refuse is visible here too.
- `lint_doc_roster.py`, `lint_doc_symbols.py` (`ava.*` refs), `lint_doc_anchors.py` (code anchors, resolved against the AST), `lint_skill_descriptions.py`, `lint_skill_md_size.py` — document / SDK / skill guards. The agent-visible docstring and `AGENTS.md`-size guards (`agent_docstrings.py`, `agents_md_size.py`) live in [[scripts/lint/docs/lint.ava.okf.md|scripts/lint/]] instead — code-convention lints, not content ones.
- `check_doc_references.py` — validates every CLI flag in the docs against the argparse tree and `scripts/*.sh` case branches, plus relative markdown links; runs on pre-commit commits that touch docs or its inputs (`pass_filenames: false`, the whole doc set), again at pre-push via `lint-prepush-artifact-freshness` when the branch only deleted such inputs (a purely deleted doc/link target never reaches a `files:`-filtered hook, on any range), and in the always-on `doc-lints` CI job (the classify-independent doc-lint family, so docs-only PRs are covered). `decisions/` is exempt entirely; `future/` only skips flag checks.
- `check_narrative_facts.py` — narrative-facts verifier: skill catalog / IM commands / IM channels mentioned in docs match the live code.

## CLI contract

The explicit-target CLI contract these lints share with `scripts/lint/`'s
suite (no-args default scope, hard-error on an unresolvable target,
out-of-repo targets scan under their absolute path) is documented once:
[[scripts/lint/docs/lint.ava.okf.md]].

Parent: [[scripts/docs/scripts.ava.okf.md|scripts]].
