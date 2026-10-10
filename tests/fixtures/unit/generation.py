"""Opt-in private generation ledgers and served gateway homes."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.config.service_read import ConfigAuthority


@pytest.fixture
def seed_write_generation() -> Callable[[Path], Any]:
    """Record an active write generation in a home's private ledger, no database.

    For code that only READS the ledger (launch delivery, bootstrap projection,
    operator consumption): the catalog side is proven on real PostgreSQL in
    tests/components/lifecycle/db_authority/. Returns the generation's secret record.
    """
    from base.cluster.authority import (
        GATEWAY_GROUP,
        RUNNER_GROUP,
        Groups,
        VerifiedGeneration,
        activate,
        create_ledger,
        read_secret,
    )
    from base.cluster.authority.ledger import begin_mint

    def seed(home: Path) -> Any:
        home = home.resolve()
        groups = Groups(gateway=GATEWAY_GROUP, runner=RUNNER_GROUP)
        create_ledger(home, owner="ava", groups=groups)
        pending = begin_mint(home, encrypt=lambda name, _pw: f"SCRAM-SHA-256$4096:c2VlZA==${name}")
        activate(home, VerifiedGeneration(pending.number, pending.credential_digest, pending.roles))
        return read_secret(home, pending)

    return seed


@pytest.fixture
def served_gateway_home(
    monkeypatch: pytest.MonkeyPatch,
    config_authority: ConfigAuthority,
    seed_write_generation: Callable[[Path], Any],
) -> Any:
    """Serve the test authority's data plane and active generation from one home.

    Build the file from the isolated boot model, never another ambient home.
    Return the generation's secret record as before.
    """
    from base import paths

    home = config_authority.env_path.parent
    config_authority.env_path.write_text(
        f"AVA_DB_URL={config_authority.service_field_value('db_url')}\n"
        f"AVA_REDIS_URL={config_authority.service_field_value('redis_url')}\n"
    )
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    return seed_write_generation(home)
