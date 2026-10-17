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
lints: [[scripts/lint/docs/lint.meiva.okf.md]].

## The linters

- `code_comments/` — closed source references, typed colocated contracts and SDK docstring projections; validates all references and migrates changed supported source files in full. See [[scripts/content_lint/code_comments/docs/code_comments.meiva.okf.md]].

- `lint_meiva_okf.py` — OKF format validation (frontmatter / size / wikilink; rules and contracts: [[scripts/content_lint/docs/lint-meiva-okf.meiva.okf.md]]); reuses `../../codegen/build_okf_data.py`'s wikilink resolver so linting a subtree does not false-positive on cross-domain links. `files:`-filtered at pre-commit; `lint-prepush-artifact-freshness` re-runs it (`--all-files`) at pre-push when the branch only deleted `.meiva.okf.md` inputs, since a `files:`-filtered hook never sees a purely deleted one on any range.
- `lint_no_tailnet.py` — bans 100.64.0.0/10 host literals; allows CIDR, frozen `docs/decisions/`, and `# tailnet-ip-ok:` test lines. Pre-commit + `repo-language` CI (2026-08-03/04 Gateway-URL, 2026-08-20 public-repo rulings).
- `lint_no_cjk.py` — repo-wide whole-repo scan (git ls-files) banning raw CJK outside the documented i18n-locale exemption (user ruling 2026-08-27).
- `../../../meizel/lint_common.py` — tracked files, target resolution, UTF-8 reads, and `file:line` output shared by `lint_no_cjk.py` and `lint_no_tailnet.py`.
- `lint_migrations.py` — timestamp-id + applied-set scheme checks: filename format, unique names, no merged migration modified/deleted/renamed (against the `origin/main` merge-base or `--base`), `db/schema.sql` baseline/folded-migration stamping, and a later-drop plan for every `*_backfill_*` snapshot table; **no expand-contract check** (that's a documentation discipline, not lint).
- `lint_audit_record.py` — every `category=audit` emit site must record the event to Postgres (`record_audit` / `record_audit_standalone`, `base/telemetry/audit_events.py`) before emitting it; sites that predate the primitives are an exact `path::function -> count` map in the script (growth and shrinkage both fail).
- `check_cross_branch_migrations.py` — tripwire: fails if the single-branch premise this repo's migrations rest on breaks. Its required host runs non-interactive Git with a 60-second query budget; an expired budget leaves the premise unverified and exits with failure.
- `lint_core_content_manifests.py` — every builtin plugin ships a manifest, and core-content manifest ranges + `requires_commit` hold against the derived host version.
- `lint_doc_roster.py` verifies [the service inventory](../../../services/supervision/roster/docs/service-roster.md) and the healthcheck roster. `lint_doc_symbols.py` (`meiva.*` refs), `lint_doc_anchors.py` (code anchors, resolved against the AST), `lint_skill_descriptions.py` (rules and contracts: [[scripts/content_lint/docs/lint-skill-descriptions.meiva.okf.md]]), `lint_skill_md_size.py` — document / SDK / skill guards. The agent-visible docstring and `AGENTS.md`-size guards (`agent_docstrings.py`, `agents_md_size.py`) live in [[scripts/lint/docs/lint.meiva.okf.md|scripts/lint/]] instead — code-convention lints, not content ones.
- `check_doc_references.py` — validates every CLI flag in the docs against the argparse tree and `scripts/*.sh` case branches, plus relative markdown links and OKF wikilink targets (including label aliases) (must resolve to an OKF node); runs on pre-commit commits that touch docs or its inputs (`pass_filenames: false`, the whole doc set), again at pre-push via `lint-prepush-artifact-freshness` when the branch only deleted such inputs (a purely deleted doc/link target never reaches a `files:`-filtered hook, on any range), and in the always-on `doc-lints` CI job (the classify-independent doc-lint family, so docs-only PRs are covered). `docs/decisions/` is exempt entirely; `future/` only skips flag checks.
- `check_narrative_facts.py` — narrative-facts verifier: skill catalog / IM commands / IM channels mentioned in docs match the live code.

- `lint_panel_jargon.py` — user-facing panel gate (`to_user.md`): reports internal identifiers, repository paths, batch codewords and unglossed acronyms in the body, with the evidence area exempt; rules and budgets live in `panel_jargon_rules.toml`. Read-only; non-zero exit on a hard finding or an over-budget body. Rules, contract and self-test: [[scripts/content_lint/docs/panel-jargon.meiva.okf.md]].
## `lint_skill_md_size.py`

SKILL.md files are loaded into agent context; a file that grows too large spends
that budget on content the current task may not need. The remedy is the same
progressive disclosure as AGENTS.md's: a root SKILL.md serves as an index and
sub-skills carry the depth. The ceiling applies per file — a skill directory
with sub-skills each under the ceiling is correctly structured — and the scan
reaches the built-in, plugin and project skill trees at both the root and
sub-skill level, reporting a path that is reachable through a linked tree once,
at its real location.

```meiva-contract
id = "skill-md-size-ceiling"
kind = "invariant"
statement = "Every repo-shipped SKILL.md is measured in lines against two repo-wide constants, per file: at or below the transitional floor the lint prints nothing; past the floor and up to the hard ceiling it prints one non-blocking note per file; past the hard ceiling it prints one error per file and exits 1. A path reachable through a linked tree is reported once at its real location, and the constants move only as a deliberate change in the same PR, justified in the commit message."
code = ["scripts/content_lint/lint_skill_md_size.py:_TRANSITIONAL_FLOOR", "scripts/content_lint/lint_skill_md_size.py:_HARD_CEILING", "scripts/content_lint/lint_skill_md_size.py:_skill_md_files", "scripts/content_lint/lint_skill_md_size.py:main"]
tests = ["scripts/content_lint/tests/test_lint_skill_md_size.py::test_a_skill_at_or_below_the_floor_is_silent", "scripts/content_lint/tests/test_lint_skill_md_size.py::test_the_transitional_zone_prints_one_note_and_passes", "scripts/content_lint/tests/test_lint_skill_md_size.py::test_the_hard_ceiling_itself_still_only_notes", "scripts/content_lint/tests/test_lint_skill_md_size.py::test_one_line_past_the_hard_ceiling_fails", "scripts/content_lint/tests/test_lint_skill_md_size.py::test_a_three_deep_sub_skill_is_scanned", "scripts/content_lint/tests/test_lint_skill_md_size.py::test_plugin_and_project_skills_are_scanned", "scripts/content_lint/tests/test_lint_skill_md_size.py::test_a_skill_reachable_through_a_linked_tree_is_reported_once"]
```

## CLI contract

The explicit-target CLI contract these lints share with `scripts/lint/`'s
suite (no-args default scope, hard-error on an unresolvable target,
out-of-repo targets scan under their absolute path) is documented once:
[[scripts/lint/docs/lint.meiva.okf.md]].

`lint_no_cjk.py` and `lint_no_tailnet.py` also expose `main(argv, *, repo_root)`
for callers scanning an explicitly chosen repository. Without `repo_root`, both
use their own checkout, including standalone CLI invocation. The public entry
owns target validation, default Git-index scope, exclusions, diagnostics, and
exit status; file-scanning algorithms remain private.

Parent: [[scripts/docs/scripts.meiva.okf.md|scripts]].
