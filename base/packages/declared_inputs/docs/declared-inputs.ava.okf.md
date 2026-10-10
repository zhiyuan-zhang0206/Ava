---
type: doc
title: "Declared Runtime Inputs"
description: '`base/packages/declared_inputs` imports a runtime-chosen module or reads a runtime-chosen path only inside a literal repository domain, so PR test selection can see the dependency and the runtime enforces it.'
tags:
- base
- library
- testing
---

# Declared Runtime Inputs

## What it is

`base/packages/declared_inputs/__init__.py` holds doors for inputs chosen at runtime:

- `declared_import(module, within=...)` imports a module;
- `declared_spec(module, within=...)` probes whether a module exists (`importlib.util.find_spec`);
- `declared_path(path, within=...)` returns a path that the caller then reads;
- `supplied_path(path)` marks a path its caller chose, such as a CLI argument. It has no
  domain and no runtime check: the test that supplies the path names it in its own source,
  and an operator may point a command at any file.

`within` is a literal tuple of patterns: dotted module patterns, or repository-relative
path patterns. `*` matches within one segment and `**` spans any number of segments.

## Why

PR test selection ([test-selection](../../../../docs/conventions/engineering/test-selection.md))
builds a static runtime-dependency graph. A name built at runtime, such as
`f"ava_builtins.plugins.{name}.metrics"` or `Path(skill["path"]) / "SKILL.md"`, has no static
target. Without a declaration, every test that reaches such a site must run for every change.
The shared fact collector (`scripts/structure/imports/declared.py`) expands each literal
domain into the checkout's matching modules or files, using the same `matches` rule as the
runtime check.

## Rules

- Only repository targets are checked:
  - a module counts when its top-level package is a checkout directory;
  - a path counts when it resolves inside the checkout, outside `.git`, `.venv`,
    `__pycache__` and `node_modules`.
- Installed dependencies, synthetic `sys.modules` entries, `$AVA_HOME` state and temporary
  files pass through unchanged.
- A repository target outside its domain raises `ValueError` before anything is imported
  or returned. Callers that wrap import failures (ava_root probes and wiring) report it as
  their own resolution error.
- An empty domain declares that the input is never a repository file or module.
- `within` must be literal text at the call site, or a single module-level binding to a
  literal tuple. Anything else stays an unknown input for test selection.
- Path-scoped fixture readers (`tests/fixtures/path_scopes.py`,
  `scripts/structure/imports/fixture_scopes.py`) do not use these doors. Their inputs are
  bound per scope by `scripts/ci/test_impact.py`.
