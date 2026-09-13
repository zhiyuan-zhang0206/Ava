# Host versioning — derived from the commit, no bump discipline

Ava's host version is the date axis of the running build, derived from the
checkout's commit — never a hand-maintained number. This page is the policy
the content-channel gates compare against (task #2915 design §5.5; landed with
tasks #2915 / #3267).

## The version

- **Bare form:** `YYYY.M.D` from the commit date of the checkout's HEAD
  (`shared/host_version.py:host_version`) — e.g. `2026.9.13`. This is the
  string every gate compares, so it always exists on every checkout-mode
  machine, advances by itself, and needs no release process.
- **Display form:** `YYYY.M.D+g<short-sha>` (`host_version_display`) — for
  humans only: `ava packages status`, log lines. Gates never see the suffix.
- **Fallback:** a checkout-less runtime (wheel install) falls back to
  `[project].version` in `pyproject.toml`. The hand-maintained number is
  vestigial (`0.1.5` since the initial public release) and read by nothing in
  the deploy path; it survives purely so the gate still has a value off a git
  checkout. When neither exists the caller surfaces "unknown" — never a
  guessed number a gate would trust.

## What packages declare

`ava-plugin.json` (spec: [`plugin-spec-v2.md`](plugin-spec-v2.md)) carries the
package's compatibility claims. A skill package may carry the same manifest
beside its `SKILL.md`; plugin bundles keep the plugin-level manifest as the
single source of truth for their bundled skills.

- **`engines.ava`** — a semver range (`>=A,<B`; either side unbounded),
  compared against the bare derived host version. Min-and-max bounds exist in
  the range algebra; declare a max only when you mean it — the date axis
  advances every day the repo moves.
- **`requires_commit`** — the exactness layer: a commit SHA the host must
  *contain* (`git merge-base --is-ancestor`). Use it when content needs a
  fresh kernel capability; point it at the merge that introduced the
  capability. Zero bump discipline, exact within a day.
- A manifest must declare **either** `engines.ava` **or** `requires_commit`
  (both is fine). Core content that depends on a fresh kernel capability uses
  `requires_commit`; third-party content, which targets released Ava
  versions, uses date-axis `engines.ava` ranges.

## Where the gates fire

| Point | Check | On violation |
|---|---|---|
| install / upgrade (skill / plugin / mcp) | `engines` range + `requires_commit` vs this checkout | refuse, report (`cli/commands/_manifest_gate.py`) |
| content-channel refresh landing | same, against the staged tree | keep the current content, record `blocked_version`, retry after the host moves |
| runtime plugin load | `engines` vs the derived version | skip that plugin, loud report, process continues |
| runtime skill scan | `engines` vs the derived version | excluded from the catalog with a visible reason |

## No bump discipline

There is deliberately **no** "bump a version on content-facing merges" rule —
that was the rejected v2 proposal (a number nothing else maintains, enforced
by CI bookkeeping). The derived date advances by itself, and a first-class
human version exists only if the dormant dated-release pipeline
(`scripts/release_cut.py`) is revived — a separate call, not a dependency of
this policy.

## CI

`scripts/lint_core_content_manifests.py` (CI job `core-content manifests`,
pre-commit `lint-core-content-manifests`) keeps core content honest:

- every manifest under `ava_builtins/` validates, its `engines` ranges admit
  the repo's current derived version, and any `requires_commit` is an ancestor
  of HEAD (a typo'd or future SHA is red);
- every other manifest in the tree is *audited* (report-only): its ranges are
  judged against the derived version and the legacy pyproject version, so the
  derived-version switch is visible per PR instead of surfacing as a surprise.
