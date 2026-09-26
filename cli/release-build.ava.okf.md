---
type: doc
title: Committed application build input
description: Offline wheel preparation from isolated Git objects with embedded source and SQL identity; no runtime activation.
tags:
- cli
- cluster-lifecycle
---

# Committed application build input

`release_build.py` builds an application wheel from an exact commit SHA into a
new private output directory. It does not change the source checkout, prepare
a complete runtime, select a release, stop services, or access the database.
Build tools are explicit existing paths. The build is offline; missing cached
backend dependencies refuse before maintenance can begin.
Build constraints are mandatory, hash-required and privately captured; every
backend requirement must be compatible with that supplied closure. The build
receipt records the constraints digest, and changed constraints refuse.
Callers using a non-default tool cache supply `--cache-dir` explicitly, including
the CI setup action; the build does not inherit ambient tool configuration.

Git runs with an explicit environment. A private bare object view excludes
mutable source-repository attributes, config, replacement refs, and inherited
Git redirection. The retained archive supplies the source tree. The embedded
`shared/release-build.json` binds the commit, tree, archive digest, schema digest,
and required migration set. Archive SQL coverage must match the committed tree;
wheel SQL paths and bytes must match an inventory captured before the backend
runs. A build hook cannot rewrite both sides of the SQL comparison.
`capture_source` exposes this same archive mechanism to
[[cli/release_prepare/acquisition/acquisition.ava.okf.md]]; acquisition has no separate Git
snapshot implementation.

Git and build commands use the shared POSIX owned-command runner. It retains
the native process-group leader until all ordinary members finish, and confirms
bounded group closure before reaping on timeout/interruption. A parent exit
cannot erase ownership of a reparented build child or turn unfinished work into
a successful receipt.

The emitted `build-receipt.json` binds the wheel digest. Failed output directories
remain available for diagnosis and cannot be reused. `shared.runtime_release.read_application_identity`
requires an already verified runtime and rechecks the embedded member against
that manifest and the expected target. Complete manifests have an explicit
32 MiB read budget; ordinary unit receipts retain their smaller default budget.
The sole installed identity may have an exact Linux `venv/lib64` mirror because
runtime preparation materializes the venv's internal symlink. Both files must
match their manifest hashes and each other byte for byte. Extra installations,
a mirror without its primary, and conflicting identity bytes refuse.

`python -m cli.release_build --repo PATH --commit SHA --output NEW_DIRECTORY
--uv ABSOLUTE_UV --python ABSOLUTE_PYTHON --build-constraints HASHED_REQUIREMENTS`
is the preparation entry. It supplies
an application input to runtime preparation; it does not prove bootability,
database compatibility, full fleet coverage, or rollout readiness. The complete
release-context producer and normal updater entry remain separate consumers.

The Linux/macOS runtime preparation workflow uses this wheel as its application
input, then independently verifies that its committed identity survives the
complete retained image build. Real Git/process regression tests cover ambient
Git overrides, lost SQL, backend SQL rewrites, and failed output isolation.
