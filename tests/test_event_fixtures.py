"""Wire-format equivalence test (Python side).

Each SystemEvent class has a sample fixture in `tests/fixtures/events/<role>.json`
(generated + validated by `scripts/dump_event_fixtures.py`). This test:

1. Each fixture JSON must be parseable by EVENT_ADAPTER — when backend adds a
   new required field to event class and the old fixture lacks it → this test fails
   → must update fixture (re-run `.venv/bin/python scripts/dump_event_fixtures.py`)
2. The parsed event.role must equal the filename (without .json) — filename as ground-truth
   to prevent role string typos inside fixtures
3. Fixtures completely cover the SYSTEM_ROLES set — if a new role is added but fixture
   forgotten, this test fails, reminding to add
4. The same fixture is shared with the frontend's corresponding vitest test (`event-fixtures.test.ts`),
   both sides can parse = wire alignment
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.live_events import EVENT_ADAPTER, SYSTEM_ROLES

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "events"


def _all_fixture_paths() -> list[Path]:
    """All fixture JSON paths — sorted by filename to keep parametrize output stable."""
    return sorted(FIXTURES_DIR.glob("*.json"))


@pytest.mark.parametrize("fixture_path", _all_fixture_paths(), ids=lambda p: p.stem)
def test_fixture_validates_via_event_adapter(fixture_path: Path) -> None:
    """Each fixture must be parseable by EVENT_ADAPTER.validate_json, and role field = filename.

    When backend adds a required field to event class but fixture lacks it, Pydantic
    strict validation throws ValidationError, this test fails → prompting user to (1) re-run
    `scripts/dump_event_fixtures.py` to update fixture (2) sync frontend
    `event-fixtures.test.ts` per-role required field assertions.
    """
    raw = fixture_path.read_text(encoding="utf-8")
    parsed = EVENT_ADAPTER.validate_json(raw)
    assert parsed.role == fixture_path.stem, (
        f"fixture {fixture_path.name}'s role field ({parsed.role!r}) does not match "
        f"filename ({fixture_path.stem!r})"
    )

    # roundtrip: model_dump_json then validate_json is equivalent (comparing against original
    # raw would be affected by whitespace / field order, comparing via model is more robust)
    redumped = parsed.model_dump_json()
    re_parsed = EVENT_ADAPTER.validate_json(redumped)
    assert re_parsed == parsed


def test_all_system_roles_have_fixtures() -> None:
    """Fixtures completely cover SYSTEM_ROLES — if a new role is added but fixture forgotten, this test fails.

    SYSTEM_ROLES is the set that the frontend SSE channels actually forward from `shared.live_events`,
    fixtures must mirror 1:1."""
    fixture_roles = {p.stem for p in _all_fixture_paths()}
    missing = SYSTEM_ROLES - fixture_roles
    extra = fixture_roles - SYSTEM_ROLES
    assert not missing, (
        f"SYSTEM_ROLES has {len(missing)} role(s) without fixture: {sorted(missing)}. "
        f"Run `.venv/bin/python scripts/dump_event_fixtures.py` to add"
    )
    assert not extra, (
        f"fixture has role(s) not listed in SYSTEM_ROLES: {sorted(extra)}. "
        f"Either SYSTEM_ROLES is missing them, or fixtures are outdated (that role was deleted)"
    )


def test_fixture_json_is_valid_json() -> None:
    """Fixture files must be valid JSON — catch typos earlier than Pydantic validation."""
    for path in _all_fixture_paths():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise AssertionError(f"{path.name} JSON invalid: {e}") from e
