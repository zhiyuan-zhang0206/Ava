"""`scripts/lint_clock_lattice.py` — the lattice-vocabulary placement invariant.

A module-level constant whose name carries lattice vocabulary (STALL / GRACE /
REAP / BUDGET / WEDGED / NO_PROGRESS / LOCK_TTL / UPDATER_LEASE / SETTLE_TTL /
LAUNCH_CONFIRM / LEASE_TTL / LEASE_RENEW / SCAN_INTERVAL) must live in a lattice
family module, be an alias of a registered clock, or carry an explicit exemption.
A bare `_SOME_REAP_GRACE_S = 100` in a new module is the 2026-07-30 spawn
incident's seedling — the name reads as part of the lattice while nothing knows
its neighbours.
"""

from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest

_lint = importlib.import_module("scripts.lint_clock_lattice")


@pytest.fixture()
def scan_tmp(tmp_path: Path, monkeypatch) -> None:
    """Point the lint at a scratch tree so fixtures never touch the real repo.

    `_REGISTERED_CLOCKS` is pinned to a fixed set (the lint resolves it once at
    import from the real tree); `_REPO_ROOT` is redirected so fixture files are
    written to the scratch tree and nothing in the real repo is read.
    """
    monkeypatch.setattr(_lint, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(_lint, "_REGISTERED_CLOCKS", frozenset({"NO_PROGRESS_TIMEOUT_S"}))


def _write(scan_tmp, rel: str, body: str) -> Path:
    p = _lint._REPO_ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _errors(scan_tmp, rel: str, body: str) -> list[str]:
    p = _write(scan_tmp, rel, body)
    return _lint._scan_file(p)


def test_bare_lattice_clock_outside_family_is_rejected(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "ops/somewhere.py",
        """
        _SOME_REAP_GRACE_S = 100.0
        """,
    )
    assert len(errs) == 1
    assert "ops/somewhere.py:2" in errs[0]
    assert "_SOME_REAP_GRACE_S" in errs[0]


def test_family_module_definition_is_allowed(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "shared/timing.py",
        """
        CONTROLLER_SCAN_INTERVAL_S = 30.0
        """,
    )
    assert errs == []


def test_alias_of_registered_clock_is_allowed(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "ops/controllers/stalled_rollout.py",
        """
        _ROLLOUT_STALL_TIMEOUT_S: float = NO_PROGRESS_TIMEOUT_S
        """,
    )
    assert errs == []


def test_exempt_clock_is_allowed(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "shared/proc.py",
        """
        _TERMINATE_GRACE_S = 3.0
        """,
    )
    assert errs == []


def test_independent_clock_without_lattice_vocabulary_is_allowed(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "ava/_mcp_oauth.py",
        """
        _OAUTH_FLOW_TIMEOUT_S = 600.0
        """,
    )
    assert errs == []


def test_settings_field_in_class_body_is_not_scanned(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "shared/config/gateway.py",
        """
        class GatewaySettings(EnvSettings):
            launch_confirm_timeout_seconds: float = Field(
                default=45.0, alias="AVA_LAUNCH_CONFIRM_TIMEOUT_SECONDS"
            )
        """,
    )
    assert errs == []


def test_unknown_clock_alias_is_rejected(scan_tmp) -> None:
    """An alias must reference a clock that is actually registered — a typo'd
    name must not ride the alias rule."""
    errs = _errors(
        scan_tmp,
        "ops/nowhere.py",
        """
        _MY_STALL_TIMEOUT_S: float = NO_PROGRES_TIMEOUT_S
        """,
    )
    assert len(errs) == 1
