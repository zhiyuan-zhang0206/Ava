---
type: doc
title: "meizel/ — Locality Rules"
description: "The top-level meizel package: the shared lint scope vocabulary and the file-size, directory-size and Python-language rules, with the code that evaluates each."
tags:
  - locality
  - tool
---

# meizel/ — Locality Rules

## What is it

`meizel/` is the home of the repository's locality rules. It is tooling, not
application code: it depends on the standard library only and never imports an
application package. `scripts/` may import `meizel`; `meizel` never imports
`scripts`.

## Contents

- `lint_common.py` — the shared scan vocabulary of the standalone lints: the
  framework directory list, test-path detection, scan-root resolution and the
  `--only` changed-files mode. Changed-files runs widen to a full scan when
  `meizel/` itself changes, because that changes the verdict of untouched files.
- `budgets/` — the 800-line file ceiling and the 20-entry directory cap
  (`limits.py`) and what counts as a direct entry of a directory
  (`directory_budget.py`). `scripts/lint/code_structure.py` drives them.
- `owners/` — one owner per external capability: `decisions.py` holds Rule 5
  (`DECISIONS`: each design decision has exactly one owning module, every other
  site is a bypass, and a listed exemption that goes stale fails) and
  `clock_lattice.py` keeps lattice-vocabulary timing constants in the
  clock-lattice family modules or as aliases of a registered clock.
- `state/` — dependency injection rules: `ambient_state/` (Rule 9, module-level
  state read to decide and free-floating background work, with the library
  handle ratchet; see [[meizel/docs/ambient-state.ava.okf.md|ambient state]]),
  `machine_role.py` (Rule 3, the retired ambient machine-role reader),
  `no_os_environ.py` (runtime config goes through `Settings`) and
  `turn_scoped_config.py` (no per-agent config through a process global). The two
  config rules take the application's config registry as an injected argument;
  their launchers in `scripts/lint/` bind it, because `meizel` imports no
  application package.
- `language/` — restrictions on how Python is written:
  `type_checking.py` (no `if TYPE_CHECKING:` import folding outside a listed
  file), `path_imports.py` (code under `ava_builtins/` reaches other code
  through packages, never a file path) and `no_script_sibling_imports.py`
  (a script-mode file restores `sys.path` before importing a sibling).

Each rule group keeps its tests in its own `tests/` directory. The gate-level
tests that drive `scripts/lint/code_structure.py` end to end stay with that
script.
