"""The page-server roster entry keeps its kebab-case ``ServiceSpec.session``.

Task #1291: the unit name must be ``page-server`` — the module name
("page_server") differs, and a launch under the module name writes a record the CLI
cannot see or kill.
"""

from __future__ import annotations

from ops.spec import build_services


def _spec_session() -> str:
    specs = build_services()
    return next(s.session for s in specs if s.session == "page-server")


def test_spec_session_uses_kebab_case() -> None:
    specs = build_services()
    assert any(s.session == "page-server" for s in specs)
