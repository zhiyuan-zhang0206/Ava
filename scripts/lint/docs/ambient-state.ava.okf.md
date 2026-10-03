---
type: doc
title: Ambient-state lint
description: Structure Rule 9 — module-level state that code reads to decide, and free-floating background work, fail; the rule ids, the closed lists, the frozen ambient_state baseline section, the scope and the known gaps.
tags:
- scripts
- lint
---

# Ambient-state lint

Inject what is read to decide; write-only facades (a logger, a meter, a tracer) may stay global. `scripts/structure/ambient_state/` is Rule 9 of `scripts/lint/code_structure.py` (pre-commit `lint-code-structure`): for anything a module holds at import, ask whether anything reads it to decide what to do. If so it belongs to a component built in a composition root and handed in. Background work must be durable or re-derivable from durable state and run as its own service loop; a per-iteration `async with asyncio.TaskGroup()` is the sanctioned bounded parallelism. The rule text is `python-conventions.md` ("Ambient state"); the detection is `ambient_state/scan.py`, whose docstring is the authoritative list of rules and known gaps.

## Rule ids

A baseline key is `path::rule:name`; the name is the assigned name, the callee (`import-time-call`), or the enclosing function (`asyncio-task`, `thread`).

| rule | reports | target |
|---|---|---|
| `ambient-instance` | `X = Foo()` of a callee that is not wiring, a pure constant or a value class (frozen dataclass, NamedTuple, Enum, stateless class, resolved from its defining file); a holder object included | component built in a root |
| `ambient-container` | an empty mutable container, or one the module mutates (`_BUILT = [False]` flipped by a function, a registry filled at import) | component-owned state |
| `contextvar` | `ContextVar(...)` | LangGraph runtime context (`AvaContext`) |
| `global-rebind` / `foreign-rebind` | a function that rebinds its module's global; an assignment to another first-party module's attribute | injected value |
| `class-level-container` | a mutable container shared through a class attribute | per-instance state |
| `hidden-singleton` / `hidden-cache` | a module-level `lru_cache` / `cache` function without / with parameters | injected value, or a reasoned `ALLOWED` entry when it is pure |
| `import-time-call` / `import-time-read` / `host-fact` | a bare call at import; a read of settings, `os.environ`, the clock or the filesystem; a platform constant | register or read where used; inject a `Platform` |
| `asyncio-task` / `thread` | `asyncio.create_task`, `ensure_future`, `<loop>.create_task`; `threading.Thread(...)` | a service loop |

## What passes

Only the closed lists in `ambient_state/allowlist.py`, each entry with a reason, and a listed file or site that disappears fails as stale: write-only facades (`SINK_CALLEES`, and `SINK_FACADES` for the state inside the log/telemetry sinks — threads they start are still reported), framework wiring (`WIRING_CALLEES`: `FastAPI()`, `APIRouter()`, parser, `StateGraph`, locks, events, semaphores, `threading.local`), pure constants (`PURE_CALLEES`, `PURE_REPO_CALLEES`) and memoized pure functions (`ALLOWED`). `DEFERRED` exempts nothing: it annotates frozen sites whose fix waits on another redesign (today the log-throttle flags, `deferred: warning/alert redesign`). There is no inline exemption and no list for background work.

## Scope and baseline

Governed: the framework packages plus `schedules/`. Out of scope: tests, `__main__.py`, an `if __name__ == "__main__":` block, and skill scripts (`ava_builtins/skills/**`, `ava_builtins/plugins/*/**/skills/**`), which run as programs. Today's sites are frozen in the `ambient_state` section of the baseline shards as `path::rule:name -> site count`, matched exactly in both directions. Against the base revision the section is shrink-only with no pairing, so a renamed or moved site is fixed, not carried (a `git -M` rename carries keys once migrated). `locality.introduced` compares the section with itself in the change that adds the `ambient_state` package.

## Library-layer ratchet

Packages outside `DB_HANDLE_PACKAGES` are not policed per site, but `scripts/structure/ambient_state/handle_ratchet.py` counts their shim dials, self-built `Database.from_settings()` calls and self-built `EventBus.from_settings()` calls (outside `BUS_PACKAGES`) per package and freezes the counts in `scripts/structure/ambient_state/handle_ratchet_baseline.json`. A count above its frozen value fails (new code takes a handle); one below it fails until `--write` lowers the baseline; against the base revision a frozen count only falls. Threading a handle into a package lowers its count; a package at zero can join `DB_HANDLE_PACKAGES`.

## Known gaps

A per-file AST pass does not see a non-empty container mutated only from another module, a `Thread` subclass instantiated elsewhere, `run_coroutine_threadsafe` or executor submits, or whether a `<expr>.create_task` receiver is really a `TaskGroup` (any receiver not named like a loop passes). A value class is resolved from its defining file, so changing a class there can move a verdict in a file that did not change; the full gate run sees it.

Parent: [[scripts/lint/docs/lint.ava.okf.md|lint]].
