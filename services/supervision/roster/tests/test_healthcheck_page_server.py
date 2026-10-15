from __future__ import annotations

from services.supervision.roster import build_services


def _spec_session() -> str:
    specs = build_services()
    return next(s.session for s in specs if s.session == "page-server")


def test_spec_session_uses_kebab_case() -> None:
    specs = build_services()
    assert any(s.session == "page-server" for s in specs)
