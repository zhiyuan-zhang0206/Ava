# CI after going public — what the swap left open

The repo is public and the CI swap is **done**: `.github/workflows/ci.yml` runs
backend / frontend / e2e gates and shared guards on GitHub-hosted `ubuntu-24.04`.
The self-hosted lane it replaced — the Hetzner runners, their autoscaler, and the
`nightly.yml` / `build-image.yml` / `ghcr-retention.yml` workflows that fed
them — is gone. Only `setup-runner-native.sh` survived the publish as dead
weight; it was deleted 2026-08-14.

The current surface is documented in
[`.github/.github.ava.okf.md`](../.github/.github.ava.okf.md). What follows is
the part that is *not* current-state: coverage the private lane had and this one
does not.

Note that the rest of `scripts/provision/` is **not** self-hosted-runner
machinery and stays: `_lib.sh` / `database.sh` / `node.sh` / `toolchain.sh` are
package-acquisition helpers used by image/toolchain preparation, and
`install-playwright.sh` is the eval image's playwright layer.
Deleting the directory wholesale, as an earlier version of this checklist said
to, would remove those dependency-acquisition primitives.

## Gaps vs. the retired private lane

Status of the gaps left by the swap:

- ~~**No native-app job.**~~ Closed: `.github/workflows/ci-app.yml` is a
  path-filtered hosted lane for the Tauri app. It runs Rust format/clippy on
  Ubuntu, macOS, and Windows; builds an installable Android debug APK after the
  generated-project overlay; and tests the overlay and updater-manifest builder.
  Signed platform packaging remains covered by the independent `app-v*` release
  workflow.
- ~~**No `pre-commit` job.**~~ Closed: `backend-structure` runs structural
  lints and conditional explicit codegen checks, `doc-lints` covers docs-only
  PRs, and the backend/frontend jobs own the heavy checks directly. Only the
  warn-only hook-installation check is local-only; see the
  [CI runbook](../conventions/runbook.md#ci-continuous-integration).
- **No `check_cross_branch_migrations`.** `scripts/check_cross_branch_migrations.py`
  exists and nothing calls it, so migration-set drift between concurrently open
  branches is unchecked.

Closed since the swap: **job timeouts** — CI jobs carry
`timeout-minutes` (2026-08-14), so a hung job stops at its cap rather than
running to GitHub's global limit.

Dropped as moot: **the `merge_group` trigger**. Trunk does not use it in this
repository.

## Why the shape is what it is

Hosted minutes are free and unmetered for public repos and public forks, so the
cost pressure that produced the private lane's whole design — commit-modulo
sampling of full runs on main, percentage sampling of e2e on PRs, a nightly
backstop to cover what sampling skipped — is gone. Current suite classification,
backend subset selection and full merge-tree verification are documented in
the [CI overview](../.github/.github.ava.okf.md). Closing the remaining gaps must
preserve that coverage while accounting for the wall-clock cost of a longer PR wait.
