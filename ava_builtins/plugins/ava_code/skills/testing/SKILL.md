---
name: testing
description: Selects and runs pytest, ruff, pyright, vitest, eslint, and tsc checks, and judges whether tests are needed. Use after any code change and before calling coding work complete.
---

# Ava Code — testing & quality gates

## Run the full set of checks

After code changes, run the checks for what you touched — don't lean on CI as your first signal:

| Backend | Frontend |
|---------|----------|
| `pytest` — unit / integration tests | `vitest` — unit / component tests |
| `ruff check` — lint | `eslint` — lint |
| `pyright` — type check | `tsc --noEmit` — type check |

Lints, typecheck, and frontend unit tests are cheap — run them in full. Backend `pytest`: run the tests for the scope you touched; the whole suite + e2e + coverage gate is CI's job (running it all locally saturates the shared dev host). All must pass; any failure gets fixed, not ignored.

## Decide whether new tests are needed

Don't just run existing tests — actively judge:

- Added a branch / new logic → add a test covering the new path.
- Fixed a bug → add a regression test so the same bug can't reappear.
- Changed an interface signature → add / update the interface test.
- Deleted dead code → confirm no test goes flaky as a result.

**Better one test too many than one missed edge case.**
