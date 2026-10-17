---
type: doc
title: Dashboard provisioning cutover
description: The meiva-ops dashboard's render fallback, its sidecar reconciliation and the four-layer cutover check.
tags: [observability, grafana, provisioning]
---

# Dashboard provisioning

The `meiva-ops` dashboard is the one provisioning file converge generates rather
than copies. It is also the file whose absence is invisible: Grafana serves an
empty dashboard list and every readiness probe still passes. The 2026-10-17
rename lost it this way — the pre-rename rendered file was orphan-cleaned, the
render for the new name did not land, and nothing failed.

```meiva-contract
id = "dashboard-provisioning-cutover"
kind = "invariant"
code = [
  "cli/commands/observability/_lgtm_provisioning.py:_render_meiva_ops_dashboard",
  "cli/commands/observability/_lgtm_provisioning.py:_install_dashboard_fallback",
  "cli/commands/observability/_lgtm_provisioning.py:record_rendered_hash",
  "cli/commands/observability/_lgtm_provisioning.py:_prune_ghost_hashes",
  "cli/commands/observability/_lgtm_provisioning.py:verify_dashboard_provisioning",
  "cli/commands/observability/grafana_render.py:cmd_grafana_render",
  "cli/commands/observability/lgtm.py:cmd_lgtm_status",
]
tests = [
  "cli/commands/observability/tests/test_lgtm_dashboard_provisioning.py::test_render_failure_installs_the_checkout_reference",
  "cli/commands/observability/tests/test_lgtm_dashboard_provisioning.py::test_render_failure_keeps_a_matching_installed_dashboard",
  "cli/commands/observability/tests/test_lgtm_dashboard_provisioning.py::test_render_failure_without_a_usable_dashboard_fails_converge",
  "cli/commands/observability/tests/test_lgtm_dashboard_provisioning.py::test_verify_accepts_a_healthy_station",
  "cli/commands/observability/tests/test_lgtm_dashboard_provisioning.py::test_verify_reports_each_broken_layer",
  "cli/commands/observability/tests/test_lgtm_dashboard_provisioning.py::test_verify_reports_grafana_that_cannot_be_read",
  "cli/commands/observability/tests/test_lgtm_dashboard_provisioning.py::test_dashboard_folder_matches_the_provisioning_configuration",
  "cli/commands/observability/tests/test_grafana_render_cmd.py::test_render_cmd_force_records_the_dashboard_hash",
  "cli/commands/observability/tests/test_grafana_render_cmd.py::test_render_cmd_records_the_hash_when_the_file_is_already_in_sync",
  "cli/commands/observability/tests/test_lgtm_dashboard_provisioning.py::test_converge_drops_sidecar_entries_with_no_rendered_file",
]
statement = """
A run of converge never leaves the station without a `meiva-ops` dashboard. When the render fails, the installed file is kept only if it is that same dashboard (`uid` = `meiva-ops-main`, at least one panel); anything else — a pre-rename file, a half-written one, no file — is replaced by the checkout's reference copy, which records itself in `provisioning-hashes.json` under `$dashboard-source: reference`. Every failed render emits `lgtm_dashboard_render_failed` with `attributes.outcome` naming what it did: `kept`, `reference`, or `failed`. With neither a matching installed file nor a usable reference, converge fails instead of shipping zero dashboards. The sidecar describes the rendered tree, so converge drops every entry naming no file under the provisioning directory: a rename's leftover `ava-` entry cannot outlive the file it described and leave the cutover check permanently red. `meiva lgtm render` records the hash whenever the installed bytes already equal the render, and `--force` records what it writes and resets the source marker to `render`, so no path leaves the sidecar describing bytes other than the installed ones. `verify_dashboard_provisioning` is the cutover check, in four layers: the file parses, carries panels and the fixed uid; Grafana serves that uid from the `Meiva` folder; the sidecar names the dashboard, marks a render rather than the reference, and keeps no `ava-` entry; no pre-rename Grafana folder survives. `meiva lgtm status` prints each failing layer and exits non-zero, because this failure is a silent absence that only a check returning an error can catch. It reports an unreachable Grafana as a failure even on a host that ran `meiva lgtm off` while its roles still declare `observability-station`: the declaration says the host serves the dashboard, and a stopped backend is that claim unmet, not an absence the check should excuse.
"""
```
