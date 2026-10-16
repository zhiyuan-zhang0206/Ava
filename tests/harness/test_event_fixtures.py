from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.events.live.projection import EVENT_ADAPTER, SYSTEM_ROLES

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures" / "events"


def _all_fixture_paths() -> list[Path]:
    return sorted(FIXTURES_DIR.rglob("*.json"))


@pytest.mark.parametrize("fixture_path", _all_fixture_paths(), ids=lambda p: p.stem)
def test_fixture_validates_via_event_adapter(fixture_path: Path) -> None:
    raw = fixture_path.read_text(encoding="utf-8")
    parsed = EVENT_ADAPTER.validate_json(raw)
    assert parsed.role == fixture_path.stem, (
        f"fixture {fixture_path.name}'s role field ({parsed.role!r}) does not match "
        f"filename ({fixture_path.stem!r})"
    )

    redumped = parsed.model_dump_json()
    re_parsed = EVENT_ADAPTER.validate_json(redumped)
    assert re_parsed == parsed


def test_all_system_roles_have_fixtures() -> None:
    fixture_roles = {p.stem for p in _all_fixture_paths()}
    missing = SYSTEM_ROLES - fixture_roles
    extra = fixture_roles - SYSTEM_ROLES
    assert not missing, (
        f"SYSTEM_ROLES has {len(missing)} role(s) without fixture: {sorted(missing)}. "
        f"Run `.venv/bin/python scripts/codegen/dump_event_fixtures.py` to add"
    )
    assert not extra, (
        f"fixture has role(s) not listed in SYSTEM_ROLES: {sorted(extra)}. "
        f"Either SYSTEM_ROLES is missing them, or fixtures are outdated (that role was deleted)"
    )


def test_fixture_json_is_valid_json() -> None:
    for path in _all_fixture_paths():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise AssertionError(f"{path.name} JSON invalid: {e}") from e
