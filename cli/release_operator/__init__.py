"""`ava cluster release prepare / request / adopt / exclude / status` — operator verbs.

Thin wiring over the release machinery: `prepare` calls
`cli.release_prepare.prepare_image` unchanged; `request` builds the fleet's
`cli.release_fleet.request.FleetRequest` on the gateway home (a single box is
a fleet of one); `adopt` performs the first-activation sequence
(`activate_release` + `install_steady`); `exclude` records an operator
exclusion in a held fleet journal; `status` reads the selection, the
published fleet release state and the operation journal. None of them
decides a release: the coordinator (`cli.release_fleet.coordinator`) does.
"""

from __future__ import annotations
