---
type: doc
title: "Test Layout"
description: "Responsibility groups for package tests, cross-package contracts and test infrastructure."
tags:
- evaluation
- quality-assurance
---

# Test Layout

Package tests live beside the code they prove. Cross-package contracts are grouped by
domain under `tests/components/`; cross-cutting checks live in `tests/contracts/`,
and test-infrastructure contracts live in `tests/harness/`. Shared fixture owners
remain in `tests/fixtures/` and `tests/path_scoped/`. The normal 20-child directory
budget applies to tests and fixture metadata as well as source code.

### Test coverage scope
- `tests/components/agent/` — agent core (loop, graph, messages, state, hooks)
- `tests/components/ava/` — SDK surface (ava.* namespace)
- `tests/components/gateway/` — API gateway
- `tests/components/cli/` — command-line tools
- `tests/components/base/` — base library (`base/packages/*/tests/` holds the tests of `base/packages`)
- `tests/components/services/` — backend services
- `tests/components/ops/` — agent drain, pause/recovery, deployment holds, health inventory,
  service roster and resource admission
- `tests/components/lifecycle/` — lifecycle qualification, grouped by database authority,
  native root custody and the fleet update script
  (`preview/` holds the CI preview visual gate's golden-minting contracts).
  Native platform cases keep their explicit gates; moving the tests does not
  turn a simulated result into native evidence.
- `tests/skills/` — skills
- `tests/scripts/` — release/docs/secret-rotation and repository tooling;
  cross-cutting lint contracts live at `tests/contracts/test_lint_*.py`
- `tests/ui/` — deterministic tests for applying the shell's Kotlin/XML/signing overlay to Tauri's generated Android project
- `tests/components/plugins/` — cross-plugin tests (`test_grafana_dashboard_render.py`, `test_plugin_metrics_logql.py`); a plugin's own tests live in its package
- `tests/fixtures/` — the suite's global fixture plugins ([[test-fixtures.ava.okf.md]]) plus event fixture data; `tests/factories/` — data factories

- `scripts/tests/test_test_selector.py` — synthetic-checkout contracts for
  the static PR test selector, including queue, duration, and
  process-determinism escapes; `scripts/tests/test_test_selector_owner_rules.py`
  holds the per-path owner rules and the tracked-tree completeness guard
- `tests/scripts/test_ci_test_selection.py` — workflow contracts for the
  test-selection routing: the single mode switch, the enforced-subset gate,
  the shadow fallback, and the matching non-flaky pytest comparison


Suite entry points and CI ownership: [[tests.ava.okf.md]].
