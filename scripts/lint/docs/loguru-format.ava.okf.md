---
type: doc
title: Loguru format lint
description: loguru takes `{}` fields and has no `exc_info` parameter (tracebacks need `logger.opt(exception=True)`); stdlib logging takes `%s` — what scripts/lint/diagnostics/loguru_format.py flags, its hook, opt-out and scope.
tags:
- scripts
- lint
---

# Loguru format lint

`scripts/lint/diagnostics/loguru_format.py` (pre-commit `lint-loguru-format`) keeps each
logger's message format honest, told apart by where the name comes from, not by
file. loguru formats with `str.format`: `logger.warning("x %s", x)` logs a
literal `%s` and drops the arguments; stdlib `logging` formats with `%`:
`_log.info("x {}", x)` raises `TypeError` at emit. Flags both directions, on
loguru loggers (`base.log` / `loguru`) and `logging.getLogger(...)` ones.

It also flags `exc_info=` on a loguru call: loguru has no such parameter — the
kwarg rides the record's `extra` and the traceback is silently lost — so the
traceback belongs on the call as `logger.opt(exception=True)` (or
`logger.opt(exception=exc)` for an exception object; stdlib loggers keep
`exc_info`; task #4979).

Opt-out `# log-format-ok: <reason>`; scope `lint_common.FRAMEWORK_DIRS` +
`scripts/`. The authoritative rule text is the script's module docstring.

Each source has one breadth-first AST node sequence, shared by the import,
derived-binding, ambiguous-name and call analyses. Derived bindings still resolve
to a fixed point, and every analysis sees the original traversal order.

The required CI `backend-structure` job runs `lint-loguru-format` and
`lint-logger-add-diagnose` with `pre-commit run --all-files` on both SELECTED and
FULL backend test paths. These hooks own repository-wide logging compliance.
Tests in `scripts/lint/tests/logging/` exercise the AST rules and explicit-target
CLI contracts without repeating the whole-repository scans inside pytest.
The same required job runs these contracts in the static process environment,
with Postgres/Redis refused and the suite's home and host-effect guards retained.

Parent: [[scripts/lint/docs/lint.ava.okf.md|lint]].
