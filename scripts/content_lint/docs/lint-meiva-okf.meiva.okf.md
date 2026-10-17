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

The lint scans .meiva.okf.md files under each target with hidden directories skipped except .github/, whose overview node lives inside it, mirroring the graph builder's file walk; the collected universe is bundle-relative posix paths.

## Node schema

A node is a `.meiva.okf.md` file (a wrong extension fails first). Its
frontmatter must declare `type: doc` or `type: memory`, a non-empty `title`
and `description`, a `tags` list of strings when present, and none of the
forbidden keys — categorization belongs to `tags`, hierarchy to the
filesystem. A missing or invalid YAML block is reported and skips the
remaining field and body checks.

A node is a .meiva.okf.md file whose frontmatter declares a type of doc or memory with a non-empty title and description, tags as a list of strings when present, and none of the forbidden keys; a wrong extension fails first, and a missing or invalid YAML block is reported and skips the remaining field and body checks.

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

A directory's overview node has exactly one canonical position — inside the directory it describes, in its docs/ layer when layered, judged on the logical path — so a sibling <dir>.meiva.okf.md is reported with the path to move it to; a directory that exists only in logical space counts the same way, and matching is repo-relative so a checkout directory's own name cannot misfire.

Every node outside .github/ sits in a docs/ layer beside the code it describes; a node outside one is reported with the location it belongs in — docs/ under the nearest directory above it that directly holds code, else under the node's own directory.

Parent: [[scripts/content_lint/docs/content_lint.meiva.okf.md]].
