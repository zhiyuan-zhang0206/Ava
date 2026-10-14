"""Opt-in private generation ledgers: an active write generation without a database."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


def seed_active_generation(home: Path) -> Any:
    """Record an active write generation in `home`'s private ledger, no database.

    The same ledger, pending mint and activation `ava start` performs at birth, minus
    the catalog roles (their proof is on real PostgreSQL in
    tests/components/lifecycle/db_authority/). Returns the generation's secret record,
    whose `.api` carries the gateway and runner machine API tokens.
    """
    from base.host.db_authority.ledger import activate, begin_mint, create_ledger, read_secret
    from base.host.db_authority.model import GATEWAY_GROUP, RUNNER_GROUP, Groups, VerifiedGeneration

    home = home.resolve()
    groups = Groups(gateway=GATEWAY_GROUP, runner=RUNNER_GROUP)
    create_ledger(home, owner="ava", groups=groups)
    pending = begin_mint(home, encrypt=lambda name, _pw: f"SCRAM-SHA-256$4096:c2VlZA==${name}")
    activate(home, VerifiedGeneration(pending.number, pending.credential_digest, pending.roles))
    return read_secret(home, pending)


@pytest.fixture
def seed_write_generation() -> Callable[[Path], Any]:
    """`seed_active_generation` as a fixture.

    For code that only READS the ledger (launch delivery, bootstrap projection,
    operator consumption). Returns the generation's secret record.
    """
    return seed_active_generation
