---
type: doc
title: "meizel/ — path_scopes mounts"
description: "A path_scopes.toml mounts a pytest plugin by dotted name; each mounted module is one plugin fact edge from the TOML to the module's file, judged like an import."
tags:
  - locality
  - tool
---

# meizel/ — path_scopes mounts

## What is it

A `path_scopes.toml` in a directory maps a dotted module name to `"."` (the directory) or to
test files in it. The test suite registers that module as a pytest plugin for those paths when it
collects them. The module is loaded from a string, so no `import` records the dependency, and a
directory could mount a plugin from a layer above it without the checker seeing it.

The checker reads every tracked `path_scopes.toml` and records one fact edge, of the same kind as
a literal `pytest_plugins`, from the TOML file to the mounted module's file: one edge per
module, however many paths it governs. A mount with no paths governs nothing and is no edge.

## How it is judged

Like an import: the edge must be declared in the manifest of the lowest directory that contains
both ends, enter a directory through its exports, avoid underscore modules and close no cycle.
A directory that mounts a plugin of the tests above it is therefore a back-edge, recorded as
debt in the owning manifest, keyed by the TOML and the module
(`base/x/tests/path_scopes.toml -> tests/path_scoped/gateway_tests.py`). To repay: move the
plugin to the mounting directory's side, split the part it needs, or drop the mount.

A module that is not in the checkout is an `unresolved-import` error at the key's line, since
the suite would fail to import it. A TOML that does not parse stops the analysis. A run limited to
changed files reads only the changed `path_scopes.toml` files.

Parent: [[meizel/docs/checker/checker.ava.okf.md|the checker]].
