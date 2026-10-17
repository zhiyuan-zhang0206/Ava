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

A finding is dev-only when every lockfile node carries dev:true, and has no forward fix when npm reports none or suggests a change that is not newer than the installed package; only both together exempt it.

Each pinned-binary row reads its version constant from its owning file by an exact regex; a renamed, moved or ambiguous constant raises instead of silently dropping the row.

Parent: [[scripts/docs/scripts.meiva.okf.md]].
