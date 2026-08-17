# CI after going public — what the swap left open

The repo is public and the CI swap is **done**: `.github/workflows/ci.yml` runs
three jobs (backend / frontend / e2e) on GitHub-hosted `ubuntu-24.04`, and the
self-hosted lane it replaced — the Hetzner runners, their autoscaler, and the
`nightly.yml` / `build-image.yml` / `ghcr-retention.yml` workflows that fed
them — is gone. Only `setup-runner-native.sh` survived the publish as dead
weight; it was deleted 2026-08-14.

The current surface is documented in
[`.github/.github.ava.okf.md`](../.github/.github.ava.okf.md). What follows is
the part that is *not* current-state: coverage the private lane had and this one
does not.

Note that the rest of `scripts/provision/` is **not** self-hosted-runner
machinery and stays: `_lib.sh` / `database.sh` / `node.sh` / `toolchain.sh` are
sourced by `scripts/install.sh` (the installer a new user runs) and the
`Dockerfile`, and `install-playwright.sh` is the eval image's playwright layer.
Deleting the directory wholesale, as an earlier version of this checklist said
to, would break the installer.

## Gaps vs. the retired private lane

These were deliberate — they keep the workflow small — and each is still open:

- ~~**No native-shell job.**~~ Closed: `.github/workflows/ci-shell.yml` is a
  path-filtered hosted lane for the Tauri shell. It runs Rust format/clippy,
  checks the Android Rust target, and tests the generated-Android overlay and
  updater manifest builder. Platform packaging is covered by the independent
  `shell-v*` release workflow.
- **No `pre-commit` job.** The pre-commit config carries ~25 lint hooks
  (`lint-ava-okf`, `lint-doc-symbols`, `lint-skill-*`, `lint-fail-fast`,
  `lint-no-os-environ`, the import-linter contracts, …). CI runs only the subset
  spelled out as its own steps — ruff, the migration + clock-lattice lints,
  pyright. Every other hook is enforced only for contributors who ran
  `pre-commit install`, which for an outside contributor is *nobody by default*.
  This is the largest of the gaps: it is the one where a PR can be green and
  still violate a rule the repo considers binding.
- **No `check_cross_branch_migrations`.** `scripts/check_cross_branch_migrations.py`
  exists and nothing calls it, so migration-set drift between concurrently open
  branches is unchecked.

Closed since the swap: **job timeouts** — all three jobs now carry
`timeout-minutes` (2026-08-14), so a hung job stops at its cap rather than
running to GitHub's global limit.

Dropped as moot: **the `merge_group` trigger**. It existed for a Mergify queue
that does not run on this repo.

## Why the shape is what it is

Hosted minutes are free and unmetered for public repos and public forks, so the
cost pressure that produced the private lane's whole design — commit-modulo
sampling of full runs on main, percentage sampling of e2e on PRs, a nightly
backstop to cover what sampling skipped — is gone. Everything runs on every PR
instead. That is why closing the gaps above is a matter of adding steps, not of
rebuilding a scheduling scheme: there is no budget to spend, only the wall-clock
cost of a longer PR wait.
