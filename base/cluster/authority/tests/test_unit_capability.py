"""Per-unit database capability for remote agent-runners (the manual delivery).

Bootstrap serves no database login. The gateway operator issues a sealed,
unit-bound bundle of the ACTIVE write generation's runner login; the runner
installs it at start and its launcher delivers it. This module holds the tamper,
binding and issue checks, which need no database; the boot pass, the launcher and the
end-to-end path against a real single-box gateway are in `test_unit_capability_*.py`
beside the code they exercise.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.cluster import authority
from base.cluster.authority import unit
from base.cluster.authority.tests.unit_capability_support import (
    _ENDPOINT,
    _MACHINE,
    _install,
    _issue,
    _no_probe,
    _open,
)
from base.cluster.authority.tests.unit_capability_support import gateway as gateway
from base.cluster.authority.tests.unit_capability_support import runner_home as runner_home
from base.deploy.progress_timeout import UNIT_BUNDLE_MAX_TTL_S

# ── sealing: tampering, keys, expiry ────────────────────────────────────────


def _flip(text: str) -> str:
    return ("A" if text[0] != "A" else "B") + text[1:]


_MUTATIONS: dict[str, Callable[[dict[str, Any]], None]] = {
    "ciphertext": lambda d: d.update(ciphertext=_flip(d["ciphertext"])),
    "truncated ciphertext": lambda d: d.update(ciphertext=d["ciphertext"][:-8]),
    "iv": lambda d: d.update(iv=_flip(d["iv"])),
    "header generation": lambda d: d["header"].update(generation=d["header"]["generation"] + 1),
    "header machine": lambda d: d["header"].update(machine="other"),
    "header home": lambda d: d["header"].update(home="/elsewhere"),
    "header expiry": lambda d: d["header"].update(expires_at=d["header"]["expires_at"] + 1e6),
}


@pytest.mark.parametrize("mutation", sorted(_MUTATIONS))
def test_a_tampered_bundle_is_refused(gateway: Path, runner_home: Path, mutation: str) -> None:
    issued = _issue(gateway, runner_home)
    assert _open(issued).capability.unit.home == str(runner_home)
    data = json.loads(issued.envelope)
    _MUTATIONS[mutation](data)
    with pytest.raises(unit.UnitCapabilityError, match="does not authenticate"):
        unit.open_bundle(json.dumps(data).encode(), issued.transport_key)
    assert unit.load_unit_capability(runner_home) is None


def test_a_bundle_opens_only_with_its_own_transport_key(gateway: Path, runner_home: Path) -> None:
    issued, other = _issue(gateway, runner_home), _issue(gateway, runner_home)
    assert issued.transport_key != other.transport_key
    with pytest.raises(unit.UnitCapabilityError, match="does not authenticate"):
        unit.open_bundle(issued.envelope, other.transport_key)
    with pytest.raises(unit.UnitCapabilityError, match="32-byte key"):
        unit.open_bundle(issued.envelope, "AAAA")
    with pytest.raises(unit.UnitCapabilityError, match="not a database capability bundle"):
        unit.open_bundle(b"{}", issued.transport_key)
    # The envelope never carries the login or the API token in clear.
    bundle = _open(issued)
    assert bundle.capability.api is not None
    for secret in (bundle.capability.password, bundle.capability.api.token):
        assert secret.encode() not in issued.envelope


def test_an_expired_bundle_is_refused(gateway: Path, runner_home: Path) -> None:
    issued = _issue(gateway, runner_home, ttl_s=60, now=time.time() - 120)
    with pytest.raises(unit.UnitCapabilityError, match="expired"):
        _open(issued)


@pytest.mark.parametrize(
    "ttl_s",
    [0.0, -1.0, math.nan, UNIT_BUNDLE_MAX_TTL_S + 1, math.inf],
    ids=["zero", "negative", "nan", "one-past-the-cap", "infinite"],
)
def test_issue_refuses_a_lifetime_outside_the_cap(
    gateway: Path, runner_home: Path, ttl_s: float
) -> None:
    with pytest.raises(unit.UnitCapabilityError, match="lifetime"):
        _issue(gateway, runner_home, ttl_s=ttl_s)


def test_issue_takes_up_to_the_capped_lifetime(gateway: Path, runner_home: Path) -> None:
    issued = _issue(gateway, runner_home, ttl_s=UNIT_BUNDLE_MAX_TTL_S, now=1_000_000.0)
    assert issued.expires_at == 1_000_000.0 + UNIT_BUNDLE_MAX_TTL_S


def test_issue_needs_an_active_generation(tmp_path: Path, runner_home: Path) -> None:
    bare = (tmp_path / "bare").resolve()
    bare.mkdir(mode=0o700)
    with pytest.raises(authority.AuthorityRefusedError, match="no database authority ledger"):
        _issue(bare, runner_home)


def test_issue_names_only_the_credential_free_endpoint(gateway: Path, runner_home: Path) -> None:
    with pytest.raises(unit.UnitCapabilityError, match="credential-free"):
        _issue(gateway, runner_home, endpoint="postgresql://ava:pw@10.0.0.7:6433/ava")


# ── install: binding, private store ─────────────────────────────────────────


def test_install_binds_the_unit_and_the_served_endpoint(
    gateway: Path, runner_home: Path, tmp_path: Path
) -> None:
    issued = _issue(gateway, runner_home)
    bundle = _open(issued)
    with pytest.raises(unit.UnitCapabilityError, match="issued for"):
        unit.install_bundle(
            runner_home, bundle, machine="other", served_endpoint=_ENDPOINT, probe=_no_probe
        )
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    with pytest.raises(unit.UnitCapabilityError, match="issued for"):
        unit.install_bundle(
            elsewhere, bundle, machine=_MACHINE, served_endpoint=_ENDPOINT, probe=_no_probe
        )
    with pytest.raises(unit.UnitCapabilityError, match="another database endpoint"):
        _install(runner_home, issued, served_endpoint="postgresql://ava@10.0.0.8:6433/ava")
    assert unit.load_unit_capability(runner_home) is None

    installed = _install(runner_home, issued)
    assert unit.unit_capability_path(runner_home).stat().st_mode & 0o777 == 0o600
    assert (unit.unit_capability_path(runner_home).parent.stat().st_mode & 0o777) == 0o700
    assert installed == unit.require_unit_capability(runner_home)
    assert installed.role == "ava_g0_runner"


# ── the runner's boot pass and launcher ─────────────────────────────────────


# ── real PostgreSQL + PgBouncer + Redis: the gateway fixture ────────────────
