---
type: doc
title: Code generation tools
description: Owners of generated schemas, frontend projections, registries and engineering artifacts.
tags: [tool, ci]
---

# Code generation tools

`build_okf_data.py` (bundle → `graph_data.json`), `serve_okf_viz.py` (local viewer, `okf-d3-template.html` — also read by `gateway/inspect/okf_graph.py`'s `/api/okf/graph`) — OKF tooling. `build_app_update_manifest.py` (Tauri archives → `latest.json`),
`build_android_release.py` (Android build number → Tauri `--config` overlay and
the `android-latest.json` update manifest), `dump_event_fixtures.py`, `dump_frontend_constants.py`, `dump_openapi.py`, `gen_config_lite_table.py`, `gen_event_registry.py`, `gen_pyright_test_environments.py` (test hosts from pytest `testpaths`; the `executionEnvironments` entries of `pyproject.toml` that hold each package's `tests/` directory to the tests type-checking standard, written between its GENERATED markers) — one generator per artifact, each with a matching `check-*-fresh.sh` or pre-commit drift gate.

The API type projection and its shared generation/freshness entry point are owned by
[[scripts/codegen/docs/types.ava.okf.md]]. OpenAPI retains descriptions; generated
TypeScript carries declarations only.

Parent: [[scripts/docs/scripts.ava.okf.md]].
