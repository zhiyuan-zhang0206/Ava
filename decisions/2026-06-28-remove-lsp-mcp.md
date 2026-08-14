# Remove LSP MCP server

**Date**: 2026-06-28
**PR**: [#87](https://github.com/ava/ava/pull/87)

## Decision

Remove all LSP MCP server references from the Ava codebase — source code,
documentation, tests, eval cases, and provision scripts. Keep only Chrome as
the default MCP server example.

## Why

- LSP (`mcp-language-server` + `pyright-langserver`) was an early experiment in
  giving agents IDE-grade code navigation. It worked — agents could find
  references, definitions, and call sites — but the cost (Go binary install,
  per-repo index warm-up, container image bloat, eval complexity) exceeded the
  value after Chrome DevTools MCP became the primary browser integration.
- The LSP eval cases (`lsp_references.py`, `lsp_references_active.py`) measured
  whether agents used the right tool — a diagnostic dimension Avas philosophy
  explicitly treats as secondary to "got the right answer." Ripgrep and
  stdlib-based approaches achieve the same result with zero infra.
- Deployment had already moved on: no machine in the fleet runs LSP, and the
  eval containers were the last place generating `mcp.json` for it.

## What changed

| Area | Change |
|------|--------|
| `ava/_mcps_daemon.py`, `ava/mcps.py` | Docstring / comment: LSP → Chrome / generic |
| `docs/current/architecture.md`, `overview.md` | Remove LSP from MCP examples |
| `future/infra/mcp-scope-and-bundling.md` | Remove LSP row from survey table + prose |
| `evals/cases/code/lsp_references*.py` | Deleted (104 lines total) |
| `evals/driver.py`, `driver_container.py` | Remove LSP from comments, OOM note, mcp.json block |
| `pyproject.toml` | Remove LSP-related pyright exclude comment |
| `scripts/provision/lsp.sh` | Deleted (30 lines) |
| `tests/ava/test_mcps.py` | Replace `lsp` → `chrome` in test fixtures |

## NOT done

- No replacement MCP server was introduced. Chrome remains the sole default.
- No machine-level daemon for Chrome was built (the headed browser runs as a
  separate service, not through the per-agent MCP daemon — see
  `future/infra/mcp-scope-and-bundling.md` for the scope model).
