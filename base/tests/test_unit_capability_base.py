"""A pure runner's boot pass with an installed unit capability: the launched service keeps its unit login over the bootstrap endpoint, a remote unit drops an inherited human secret, a forged login is replaced and refused, an admitted operator process consumes the installed login, and a runner without a capability is refused by name."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from base.cluster import authority
from base.cluster.authority import unit
from base.cluster.authority.api import API_TOKEN_ENV
from base.cluster.authority.tests.unit_capability_support import _ENDPOINT
from base.cluster.authority.tests.unit_capability_support import gateway as gateway
from base.cluster.authority.tests.unit_capability_support import runner_boot as runner_boot
from base.cluster.authority.tests.unit_capability_support import runner_home as runner_home
from base.db.connections import NoDatabaseAuthorityError, _guard_db_url
from base.host.env import bootstrap, dotenv_boot

_REPO = Path(__file__).resolve().parents[2]


def _boot_with_bootstrap() -> None:
    """The runner's boot order: the authority pass, the unit delivery (which
    supplies the fetch's API token), then the bootstrap payload (the
    credential-free endpoint)."""
    dotenv_boot._enforce_cluster_env_authority(dotenv_boot.resolve_ava_home())
    dotenv_boot.deliver_unit_authority()
    bootstrap._apply_bootstrap_values(
        "http://gateway.invalid", {"AVA_DB_URL": _ENDPOINT, "AVA_EVENTS_CHANNEL": "ava:events"}
    )


def test_a_launched_service_keeps_its_unit_login_over_the_bootstrap_endpoint(
    runner_boot: unit.UnitCapability,
) -> None:
    os.environ["AVA_PROCESS_PROFILE"] = "runner"
    os.environ.update(unit.unit_delivery(Path(runner_boot.unit.home)))
    os.environ.update(unit.unit_api_delivery(Path(runner_boot.unit.home)))
    _boot_with_bootstrap()
    assert os.environ["AVA_DB_URL"] == runner_boot.dsn
    assert os.environ[authority.GENERATION_ENV] == "0"
    assert runner_boot.api is not None and os.environ[API_TOKEN_ENV] == runner_boot.api.token
    assert dotenv_boot.db_authority_refusal() is None


def test_a_remote_unit_drops_an_inherited_human_secret(
    runner_boot: unit.UnitCapability,
) -> None:
    """Its `.env` never declares the human secret, so a copy inherited from a
    shell is dropped: remote-unit processes never hold it."""
    del runner_boot
    os.environ["AVA_CLUSTER_SECRET"] = "inherited-" + "x" * 32
    _boot_with_bootstrap()
    assert "AVA_CLUSTER_SECRET" not in os.environ


def test_a_forged_login_is_replaced_by_the_endpoint_and_refused(
    runner_boot: unit.UnitCapability,
) -> None:
    del runner_boot
    os.environ["AVA_PROCESS_PROFILE"] = "runner"
    os.environ["AVA_DB_URL"] = "postgresql://ava_g0_runner:forged@10.0.0.7:6433/ava"
    os.environ[authority.GENERATION_ENV] = "0"
    _boot_with_bootstrap()
    assert os.environ["AVA_DB_URL"] == _ENDPOINT
    refusal = dotenv_boot.db_authority_refusal()
    assert refusal is not None and "only the root launcher delivers" in refusal
    with pytest.raises(NoDatabaseAuthorityError, match="credential-free"):
        _guard_db_url(_ENDPOINT)


def test_an_admitted_operator_process_consumes_the_installed_login(
    runner_boot: unit.UnitCapability, runner_home: Path
) -> None:
    intent = runner_home / "start-intent.json"
    intent.write_text(json.dumps({"home": str(runner_home), "checkout": str(_REPO)}))
    intent.chmod(0o600)
    _boot_with_bootstrap()
    assert os.environ["AVA_DB_URL"] == runner_boot.dsn
    assert os.environ[authority.GENERATION_ENV] == "0"
    # ... and its API token, which the bootstrap fetch then presents.
    assert runner_boot.api is not None and os.environ[API_TOKEN_ENV] == runner_boot.api.token
    assert dotenv_boot.db_authority_refusal() is None


def test_a_runner_without_a_capability_is_refused_by_name(
    runner_boot: unit.UnitCapability, runner_home: Path
) -> None:
    del runner_boot
    intent = runner_home / "start-intent.json"
    intent.write_text(json.dumps({"home": str(runner_home), "checkout": str(_REPO)}))
    intent.chmod(0o600)
    unit.unit_capability_path(runner_home).unlink()
    _boot_with_bootstrap()
    assert os.environ["AVA_DB_URL"] == _ENDPOINT
    assert API_TOKEN_ENV not in os.environ
    refusal = dotenv_boot.db_authority_refusal()
    assert refusal is not None and "holds no database capability" in refusal
    assert "ava cluster db-authority issue-unit" in refusal
    with pytest.raises(NoDatabaseAuthorityError, match="issue-unit"):
        _guard_db_url(_ENDPOINT)
