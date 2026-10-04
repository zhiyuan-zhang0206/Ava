"""Regression coverage for full-candidate validation before config persistence.

The cross-field invariant these tests lean on is the sandbox one: the inner
`exec_timeout_seconds` must stay below the outer `exec_node_timeout_seconds`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from base.config.admin.candidate import EnvPatchValidation
from base.host.env import runtime_config

type EnvPatchValidator = Callable[[dict[str, object], set[str]], list[str]]


@pytest.fixture
def configured_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home whose `.env` carries a valid sandbox timeout pair."""
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    runtime_config.write_fields(
        {"exec_timeout_seconds": 300, "exec_node_timeout_seconds": 1200}, set()
    )
    return tmp_path


def _validate_env_patch() -> EnvPatchValidator:
    from base.config.admin.candidate import validate_env_patch

    return validate_env_patch


def _validate_env_patch_for_write() -> Callable[[dict[str, object], set[str]], EnvPatchValidation]:
    from base.config.admin.candidate import validate_env_patch_for_write

    return validate_env_patch_for_write


def test_patch_that_breaks_a_cross_field_invariant_is_rejected(configured_home: Path) -> None:
    """The persisted candidate must satisfy the validators that run at startup."""
    errors = _validate_env_patch()({"exec_node_timeout_seconds": 200}, set())

    assert errors
    assert any("exec_node_timeout_seconds" in error for error in errors)


def test_patch_that_keeps_the_invariant_is_valid(configured_home: Path) -> None:
    assert _validate_env_patch()({"exec_node_timeout_seconds": 900}, set()) == []


def test_unrelated_domain_patch_is_valid(configured_home: Path) -> None:
    """Validation reconstructs only the domain the patch changes."""
    assert _validate_env_patch()({"llm_model": "candidate-model"}, set()) == []


def test_unrelated_invalid_domain_does_not_reject_a_candidate(configured_home: Path) -> None:
    """A broken sandbox file value cannot poison an independent model candidate."""
    runtime_config.write_fields(
        {"exec_timeout_seconds": 1200, "exec_node_timeout_seconds": 300}, set()
    )

    assert _validate_env_patch()({"llm_model": "candidate-model"}, set()) == []


def test_removal_uses_the_field_default(configured_home: Path) -> None:
    """Removing a field falls back to its default, which the candidate is checked with."""
    runtime_config.write_fields({"exec_node_timeout_seconds": 900}, set())

    assert _validate_env_patch()({}, {"exec_node_timeout_seconds"}) == []


def test_removal_that_leaves_an_invalid_pair_is_rejected(configured_home: Path) -> None:
    """The default stands in for a removed field, so removal can still break the invariant."""
    runtime_config.write_fields(
        {"exec_timeout_seconds": 1500, "exec_node_timeout_seconds": 2000}, set()
    )

    errors = _validate_env_patch()({}, {"exec_node_timeout_seconds"})

    assert any("exec_node_timeout_seconds" in error for error in errors)


def test_removing_required_field_is_invalid(configured_home: Path) -> None:
    """A required data-plane value cannot disappear from the persisted candidate."""
    errors = _validate_env_patch()({}, {"db_url"})

    assert errors
    assert any("AVA_DB_URL" in error and "Field required" in error for error in errors)


def test_validate_or_raise_joins_candidate_errors(configured_home: Path) -> None:
    """Non-HTTP callers receive the same alias-safe candidate explanation."""
    from base.config.admin.candidate import validate_env_patch_or_raise

    with pytest.raises(ValueError, match="exec_node_timeout_seconds"):
        validate_env_patch_or_raise({"exec_node_timeout_seconds": 200}, set())


def test_current_invalid_domain_is_rejected_until_the_patch_repairs_it(
    configured_home: Path,
) -> None:
    """A same-domain edit cannot hide a pre-existing invalid timeout pair."""
    runtime_config.write_fields({"exec_node_timeout_seconds": 200}, set())
    validate_env_patch = _validate_env_patch()

    errors = validate_env_patch({"exec_output_max_chars": 40000}, set())

    assert any("exec_node_timeout_seconds" in error for error in errors)
    assert validate_env_patch({"exec_node_timeout_seconds": 900}, set()) == []


def test_empty_patch_is_trivially_valid(configured_home: Path) -> None:
    """No touched domain means no candidate reconstruction or rejection."""
    assert _validate_env_patch()({}, set()) == []


def test_stale_candidate_digest_cannot_persist_an_invalid_combination(
    configured_home: Path,
) -> None:
    """A later valid patch must retry after another candidate has changed `.env`."""
    validate_for_write = _validate_env_patch_for_write()
    raise_inner = validate_for_write({"exec_timeout_seconds": 1000}, set())
    lower_outer = validate_for_write({"exec_node_timeout_seconds": 900}, set())

    assert raise_inner.errors == []
    assert lower_outer.errors == []
    assert raise_inner.expected_digest == lower_outer.expected_digest

    runtime_config.write_fields(
        {"exec_timeout_seconds": 1000}, set(), expected_digest=raise_inner.expected_digest
    )
    with pytest.raises(RuntimeError, match="changed before owned runtime-config write"):
        runtime_config.write_fields(
            {"exec_node_timeout_seconds": 900}, set(), expected_digest=lower_outer.expected_digest
        )
