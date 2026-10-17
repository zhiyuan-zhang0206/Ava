---
type: doc
title: OKF node lint — scan universe, schema and placement
description: The scripts/content_lint/lint_meiva_okf.py tripwire — which files it scans and the schema/placement rules every .meiva.okf.md node must satisfy.
tags: [scripts, lint, documentation]
---

# OKF node lint — scan universe, schema and placement

`lint_meiva_okf.py` validates every `.meiva.okf.md` node against the OKF format.
This node covers the scan and where a node must sit and what it must declare;
the size, wikilink and glued-marker checks are
[[scripts/content_lint/docs/lint-meiva-okf-size-and-links.meiva.okf.md]].

Run it with `.venv/bin/python scripts/content_lint/lint_meiva_okf.py
[--fix] [paths...]`; an explicit path that does not exist is an error rather
than the empty-scope message — the shared explicit-target CLI contract:
[[scripts/lint/docs/lint.meiva.okf.md]]. It runs `files:`-filtered at
pre-commit, in the `doc-lints` CI job, and again at pre-push
(`lint-prepush-artifact-freshness`) when a branch only deleted node inputs.

## Scan universe

Files are found by walking each target for `*.meiva.okf.md`; hidden directories
are skipped, except `.github/`, whose overview node lives inside it — the same
universe the graph builder walks, so linting a subtree does not false-positive
on cross-domain links. Paths are collected as bundle-relative posix paths.

```meiva-contract
id = "okf-node-universe"
kind = "invariant"
statement = "The lint scans .meiva.okf.md files under each target with hidden directories skipped except .github/, whose overview node lives inside it, mirroring the graph builder's file walk; the collected universe is bundle-relative posix paths."
code = ["scripts/content_lint/lint_meiva_okf.py:find_files", "scripts/content_lint/lint_meiva_okf.py:collect_all_paths"]
tests = ["scripts/content_lint/tests/test_lint_meiva_okf.py::test_find_files_skips_hidden_dirs_except_github"]
```

## Node schema

A node is a `.meiva.okf.md` file (a wrong extension fails first). Its
frontmatter must declare `type: doc` or `type: memory`, a non-empty `title`
and `description`, a `tags` list of strings when present, and none of the
forbidden keys — categorization belongs to `tags`, hierarchy to the
filesystem. A missing or invalid YAML block is reported and skips the
remaining field and body checks.

```meiva-contract
id = "okf-node-schema"
kind = "invariant"
statement = "A node is a .meiva.okf.md file whose frontmatter declares a type of doc or memory with a non-empty title and description, tags as a list of strings when present, and none of the forbidden keys; a wrong extension fails first, and a missing or invalid YAML block is reported and skips the remaining field and body checks."
code = ["scripts/content_lint/lint_meiva_okf.py:ALLOWED_TYPES", "scripts/content_lint/lint_meiva_okf.py:FORBIDDEN_KEYS", "scripts/content_lint/lint_meiva_okf.py:_required_field_errors", "scripts/content_lint/lint_meiva_okf.py:_tags_errors", "scripts/content_lint/lint_meiva_okf.py:_forbidden_key_errors", "scripts/content_lint/lint_meiva_okf.py:lint_file"]
tests = ["scripts/content_lint/tests/test_lint_meiva_okf.py::test_e001_rejects_a_non_node_extension", "scripts/content_lint/tests/test_lint_meiva_okf.py::test_a_missing_frontmatter_fails_before_any_field_check", "scripts/content_lint/tests/test_lint_meiva_okf.py::test_type_must_be_doc_or_memory", "scripts/content_lint/tests/test_lint_meiva_okf.py::test_title_and_description_are_required", "scripts/content_lint/tests/test_lint_meiva_okf.py::test_tags_must_be_a_list_of_strings", "scripts/content_lint/tests/test_lint_meiva_okf.py::test_forbidden_frontmatter_keys_are_reported", "scripts/content_lint/tests/test_lint_meiva_okf.py::test_type_error_names_the_allowed_types_deterministically"]
```

## Placement

A directory's overview node has exactly one canonical position: inside the
directory it describes (user ruling 2026-08-12, "put it inside the folder"),
judged on the logical path, so a node in a package's `docs/` layer counts as
sitting where the layer sits. A sibling `<dir>.meiva.okf.md` would be an orphan
(the hierarchy never resolves it), so it is reported with the canonical path
to move it to; a directory that exists only in logical space (a doc-only
directory keeps its nodes in `<package>/docs/<dir>/`) counts the same way, and
matching is repo-relative, so a checkout directory's own name cannot misfire.

Every node outside `.github/` sits in a `docs/` layer beside the code it
describes; a node outside one is reported with the location it belongs in —
`docs/` under the nearest directory above it that directly holds code (a known
code suffix, `SKILL.md` or `package.json`), else under the node's own
directory.

```meiva-contract
id = "okf-overview-position"
kind = "invariant"
statement = "A directory's overview node has exactly one canonical position — inside the directory it describes, in its docs/ layer when layered, judged on the logical path — so a sibling <dir>.meiva.okf.md is reported with the path to move it to; a directory that exists only in logical space counts the same way, and matching is repo-relative so a checkout directory's own name cannot misfire."
code = ["scripts/content_lint/lint_meiva_okf.py:_dual_node_error", "scripts/content_lint/lint_meiva_okf.py:_placement_errors"]
tests = ["scripts/content_lint/tests/test_lint_meiva_okf.py::test_e009_reports_a_sibling_overview_beside_its_directory", "scripts/content_lint/tests/test_lint_meiva_okf.py::test_e009_reports_a_layered_sibling_overview", "scripts/content_lint/tests/test_lint_meiva_okf.py::test_e009_accepts_overviews_inside_their_directory", "scripts/content_lint/tests/test_lint_meiva_okf.py::test_e009_reports_a_sibling_beside_a_folded_doc_only_directory"]
```

```meiva-contract
id = "okf-docs-layer"
kind = "invariant"
statement = "Every node outside .github/ sits in a docs/ layer beside the code it describes; a node outside one is reported with the location it belongs in — docs/ under the nearest directory above it that directly holds code, else under the node's own directory."
code = ["scripts/content_lint/lint_meiva_okf.py:_LAYER_EXEMPT", "scripts/content_lint/lint_meiva_okf.py:_CODE_SUFFIXES", "scripts/content_lint/lint_meiva_okf.py:_CODE_NAMES", "scripts/content_lint/lint_meiva_okf.py:_layer_home", "scripts/content_lint/lint_meiva_okf.py:layer_error", "scripts/content_lint/lint_meiva_okf.py:_placement_errors"]
tests = ["scripts/content_lint/tests/test_lint_meiva_okf.py::test_e014_names_the_layer_beside_the_nearest_code", "scripts/content_lint/tests/test_lint_meiva_okf.py::test_e014_keeps_the_subdirectory_below_the_layer", "scripts/content_lint/tests/test_lint_meiva_okf.py::test_e014_falls_back_to_the_nodes_own_directory", "scripts/content_lint/tests/test_lint_meiva_okf.py::test_e014_blocks_the_gate"]
```

Parent: [[scripts/content_lint/docs/content_lint.meiva.okf.md]].
