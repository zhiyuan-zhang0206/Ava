"""`services.healthchecks.page_server` respawn-session regression guard.

Task #1291: the respawn session name must match ``ServiceSpec.session``
("page-server", kebab-case) — the module name ("page_server") differs, and a
respawn under the module name writes a session record the CLI cannot see or kill.
"""

from __future__ import annotations

from ops.spec import build_services


def _spec_session() -> str:
    specs = build_services()
    return next(s.session for s in specs if s.session == "page-server")


def test_spec_session_uses_kebab_case() -> None:
    specs = build_services()
    assert any(s.session == "page-server" for s in specs)
