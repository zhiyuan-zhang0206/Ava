---
type: doc
title: "Weekly dependency audit"
description: "The weekly report over locked dependencies, npm audit and pinned binaries: its three sections, the single tracked issue, and the dev-only exemption that keeps unfixable tooling findings out of the actionable set."
tags:
  - ci
  - security
  - dependencies
---

# Weekly dependency audit

`scripts/ci/dependency_audit.py` runs weekly (and on demand) from
`.github/workflows/dependency-audit.yml`. One markdown report carries three
sections into one tracked GitHub issue, found by its marker and updated in
place, then closed when a run comes back clean:

- Python: `uv audit` over `uv.lock`; the JSON carries no severity, so each
  advisory id is graded through OSV (GitHub's reviewed severity) and an
  advisory OSV cannot grade is listed as `unknown`.
- Frontend: `npm audit` over `ui/web/package-lock.json`.
- Pinned binaries: the version constants Meiva downloads or installs outside the
  lockfiles (zonky Postgres, pgvector, wal-g, otelcol-contrib, uv, Grafana)
  against their upstream's newest release. pgvector publishes tags, not GitHub
  releases, so its row reads tags; the zonky row compares only within the
  pinned Postgres major.

The report never changes a pin: moving one is a dependency upgrade that needs
approval. `uvx` runs the audit with the exact `UV_VERSION` pinned in
`base/host/brew_pin.py`, so the report and the installed toolchain agree.

## The dev-only exemption

A finding whose lockfile nodes are all dev-only (`"dev": true` in
`ui/web/package-lock.json`) and whose npm remediation is not a forward fix —
`fixAvailable` is `false`, or the suggested change is not newer than the
installed package — has no upgrade that fixes it. It stays listed in the
report (a run must remain auditable) but does not make the issue actionable.
Without this exemption the same unfixable dev-tooling tree, for example a
transitive dependency with no patched release, would keep the issue open
forever. A dev-only finding with a real upgrade available, and every
production finding, stay actionable.

## Contracts

```meiva-contract
id = "weekly-report-run"
kind = "invariant"
statement = "A run renders the marker-prefixed three-section report from the audit tools and the canonical pins, and runs `uv audit` with the uv version pinned in base/host/brew_pin.py."
code = ["scripts/ci/dependency_audit.py:main"]
tests = ["scripts/ci/tests/test_dependency_audit.py::test_report_runs_the_canonical_uv_pin", "scripts/ci/tests/test_dependency_audit.py::test_report_orders_by_severity_and_carries_the_marker"]
```

```meiva-contract
id = "weekly-issue-lifecycle"
kind = "invariant"
statement = "The one tracked issue is located by its marker among non-pull-request issues, updated while the run is actionable, opened when missing, and closed with a comment when clean."
code = ["scripts/ci/dependency_audit.py:sync_issue"]
tests = ["scripts/ci/tests/test_dependency_audit.py::test_actionable_run_updates_the_existing_issue_instead_of_opening_another", "scripts/ci/tests/test_dependency_audit.py::test_actionable_run_with_no_issue_opens_one", "scripts/ci/tests/test_dependency_audit.py::test_clean_run_closes_the_open_issue_and_a_clean_repo_does_nothing", "scripts/ci/tests/test_dependency_audit.py::test_issue_lookup_filters_on_the_marker_and_skips_pull_requests"]
```

```meiva-contract
id = "run-actionability"
kind = "invariant"
statement = "A run is actionable when any finding is critical, high or unknown, or any pinned binary is behind; a dev-only finding with no forward fix is listed but does not make the run actionable."
code = ["scripts/ci/dependency_audit.py:is_actionable"]
tests = ["scripts/ci/tests/test_dependency_audit.py::test_issue_is_actionable_on_high_unknown_or_a_behind_binary_only", "scripts/ci/tests/test_dependency_audit.py::test_a_dev_only_finding_without_a_forward_fix_is_listed_but_not_actionable"]
```

```meiva-contract
id = "dev-only-exemption"
kind = "invariant"
statement = "A finding is dev-only when every lockfile node carries dev:true, and has no forward fix when npm reports none or suggests a change that is not newer than the installed package; only both together exempt it."
code = ["scripts/ci/dependency_audit.py:npm_vulnerabilities", "scripts/ci/dependency_audit.py:_dev_only", "scripts/ci/dependency_audit.py:_no_forward_fix"]
tests = ["scripts/ci/tests/test_dependency_audit.py::test_a_dev_only_finding_without_a_forward_fix_is_listed_but_not_actionable", "scripts/ci/tests/test_dependency_audit.py::test_a_dev_only_finding_with_an_upgrade_stays_actionable", "scripts/ci/tests/test_dependency_audit.py::test_a_production_finding_without_a_forward_fix_stays_actionable"]
```

```meiva-contract
id = "incomplete-audit-guard"
kind = "invariant"
statement = "An audit tool exiting 1 with complete JSON findings is accepted; non-JSON output, an error payload or any other exit code raises as an incomplete audit."
code = ["scripts/ci/dependency_audit.py:run_json"]
tests = ["scripts/ci/tests/test_dependency_audit.py::test_audit_accepts_complete_findings", "scripts/ci/tests/test_dependency_audit.py::test_audit_rejects_registry_and_tool_errors"]
```

```meiva-contract
id = "osv-severity-grading"
kind = "external"
statement = "uv audit JSON carries no severity; each advisory id is graded through OSV's reviewed severity, and an advisory OSV cannot grade is unknown and counts as actionable."
source = "https://api.osv.dev/v1/vulns/"
applies_to = "OSV API v1"
code = ["scripts/ci/dependency_audit.py:osv_severity"]
tests = ["scripts/ci/tests/test_dependency_audit.py::test_severity_comes_from_the_first_id_osv_can_grade", "scripts/ci/tests/test_dependency_audit.py::test_an_ungradable_advisory_is_unknown_not_dropped"]
```

```meiva-contract
id = "pinned-binary-constants"
kind = "invariant"
statement = "Each pinned-binary row reads its version constant from its owning file by an exact regex; a renamed, moved or ambiguous constant raises instead of silently dropping the row."
code = ["scripts/ci/dependency_audit.py:PINS", "scripts/ci/dependency_audit.py:pinned_version"]
tests = ["scripts/ci/tests/test_dependency_audit.py::test_every_pinned_binary_constant_is_found_in_the_tree"]
```

```meiva-contract
id = "zonky-major-comparison"
kind = "invariant"
statement = "The zonky Postgres row compares only within the pinned major: a major bump is an expand step, not what the row reports."
code = ["scripts/ci/dependency_audit.py:zonky_latest"]
tests = ["scripts/ci/tests/test_dependency_audit.py::test_zonky_latest_stays_on_the_pinned_major"]
```

Parent: [[scripts/docs/scripts.meiva.okf.md]].
