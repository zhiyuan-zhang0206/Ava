# Going public — the public-repo CI swap

How the private repo's self-hosted CI lane becomes GitHub-hosted CI when the
repo goes public, and what is still missing before that swap is safe.

## The swap

1. Copy `deploy/ci/public-ci.yml` over `.github/workflows/ci.yml` (commit the
   replacement in the same PR that opens the repo).
2. Delete the self-hosted provisioners and runners: `scripts/provision/` and
   the Hetzner CI runners (`ops/ci_autoscale/`) become dead weight once the
   private lane is gone.
3. Re-enable the checks the public lane skips (see gaps below) and run one
   real ubuntu-hosted workflow end-to-end before flipping the repo public.

Why the swap at all: GitHub-hosted runners are free and unmetered for public
repos and public forks. The self-hosted native runners exist only because the
private repo would otherwise burn metered minutes; once public that reason is
gone, forks get CI for free on GitHub's runners. No canonical-vs-fork gating
is needed — one lane runs everywhere.

The private `ci.yml` and the public template are kept in sync by review, not
by tooling: the template lives under `deploy/` precisely so it cannot
accidentally become the active workflow before the repo is public.

## Known gaps vs the private lane (close before the swap)

The template is NOT validated until a real ubuntu run exercises its inline
provisioning (PGDG pg17, setup-uv, playwright install) — the private suite
runs on pre-provisioned native runners, so the public lane's provisioning
steps have never executed. The gaps below are deliberate today (they keep the
template small) and must be closed before the swap:

- **No `desktop` job** — the private lane builds/tests the desktop app.
- **No `pre-commit`** — the private lane runs the full pre-commit gate (~25
  lint hooks); the public lane only runs the workflow's own steps.
- **No `check_cross_branch_migrations`** — migration-set drift across branches
  is unchecked in the template.
- **No `merge_group` trigger** — Mergify queue events do not run the public
  lane as written.
- **No `timeout-minutes`** on jobs — a hung public job runs until GitHub's
  global cap.

The template's `paths-ignore` mirrors the private lane's
(`ava_builtins/skills/**`, `**/*.md`).
