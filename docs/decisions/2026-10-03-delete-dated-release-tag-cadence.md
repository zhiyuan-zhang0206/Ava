# The dated release-tag cadence is deleted

## Context

The [release-path removal](2026-09-30-remove-release-image-path.md) deleted the retained-image
release machinery. What stayed was the dated tag cadence: `scripts/ci/release_cut.py` (daily and
weekly cuts), `scripts/ci/tag_latest.py`, the tag parser `base/deploy/release/tags.py`, the
`release.yml` workflow that turned a pushed tag into a GitHub Release, and the release-notes label
map. The cadence had been dormant since the last dated tag on 2026-08-08, nothing was pushed to
origin, and no update path selects a tag: `cli/fleet_update.py` switches every unit to the commit
it is given.

## Decision

Delete the whole cadence: the cutting and tagging scripts, the tag parser, the release workflow
and label map, and their tests. `editable_install.py` and `collector_artifact.py` in the same
package are unrelated and stay. Host version stays derived from the commit; a human-facing
release identity, if one is wanted later, is designed then.

## Alternatives rejected

- **Keep it dormant for a later revival.** Dormant code is maintained code: it carried a CI
  workflow, a path-exemption in the workflow test, docs in four places and a parser with its own
  tests, all for a cadence nobody runs. Reviving it would start from the then-current release
  process, not from a 2026-07 design.
- **Keep only the tag parser.** Its sole consumer was `release_cut.py`.

## Consequences

- Releases are manual milestone tags, as before; nothing reads dated tags.
- `future/infra/core-package-update-channel.md` no longer treats a dated release as a possible
  reference point; its dormant-pipeline notes say deleted.
