"""`cli.commands._repo` is a pure re-export of the ops roster, so its definitions live once."""

from __future__ import annotations

from cli.commands import _repo
from ops import roster, spec
from ops.roster import service_spec


def test_repo_is_a_pure_reexport_of_ops_spec() -> None:
    """`_repo`'s roster names are the SAME objects as ops.spec — proving the
    definitions live once (single source), not duplicated."""
    assert _repo.build_services is roster.build_services
    assert _repo.ServiceSpec is service_spec.ServiceSpec
    assert _repo.profile_marker is service_spec.profile_marker
    assert _repo._services_for_roles is spec.services_for_capabilities
    assert _repo._services_for_roles_annotated is spec.services_for_capabilities_annotated
