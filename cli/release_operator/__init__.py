"""`ava cluster release prepare / request / adopt / status` — thin single-host
operator verbs over the existing release machinery.

Every command here wires `cli.release_prepare` (build one inactive image) and
`cli.release_transition` (the `Request`/journal/native-executor machinery
already exercised by `ava cluster update --prepared`) together for an
operator working on one host. None of it changes release-transition
semantics: `prepare` calls `cli.release_prepare.prepare_image` unchanged,
`request` builds the same `cli.release_transition.request.Request` the
release-cycle preview used to build for itself, and `adopt` performs the same
first-activation sequence (`activate_release` + `install_steady`) the preview
already exercises in `scripts/preview/release_cycle_runtime.py::initial`.

There is no fleet model yet (that is `cli/release_fleet/`, slice FC-7): a
multi-host request, a coordinator, unit journals — none of it exists. Verbs
that would need it refuse clearly rather than approximate it.
"""

from __future__ import annotations
