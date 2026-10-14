"""The operator's database factory carries the one gateway-login adoption of a process."""

from __future__ import annotations

import pytest

from cli.database import OperatorDatabaseFactory, open_operator_database, operator_database_factory

_ADOPTED = "postgresql://ava_g0_gateway:pw@db.example:5432/ava"
_OTHER = "postgresql://ava_g1_gateway:pw@db.example:5432/ava"


def _factory() -> OperatorDatabaseFactory:
    return operator_database_factory(
        local_host=lambda: "localhost",
        open_database=open_operator_database,
        gateway_url_reader=lambda: "",
    )


def test_handles_before_adoption_keep_their_url_and_later_ones_dial_the_adopted_login() -> None:
    factory = _factory()
    configured = factory.db_url()
    assert configured != _ADOPTED

    before = factory()
    dialed_before = before.direct_url()
    factory.adopt_login(_ADOPTED)
    after = factory()

    assert factory.db_url() == _ADOPTED
    assert before.direct_url() == dialed_before != _ADOPTED  # a handle keeps what it was built with
    assert after.direct_url() == _ADOPTED


def test_adopting_the_same_login_again_changes_nothing() -> None:
    factory = _factory()
    factory.adopt_login(_ADOPTED)
    factory.adopt_login(_ADOPTED)
    assert factory.db_url() == _ADOPTED


def test_a_second_different_login_is_refused_and_the_first_stays() -> None:
    factory = _factory()
    factory.adopt_login(_ADOPTED)
    with pytest.raises(RuntimeError, match="already adopted a different gateway login"):
        factory.adopt_login(_OTHER)
    assert factory.db_url() == _ADOPTED


def test_the_refusal_never_prints_either_login() -> None:
    factory = _factory()
    factory.adopt_login(_ADOPTED)
    with pytest.raises(RuntimeError) as refused:
        factory.adopt_login(_OTHER)
    assert "ava_g0_gateway" not in str(refused.value) and "ava_g1_gateway" not in str(refused.value)


def test_one_factory_adopting_leaves_another_on_the_settings() -> None:
    adopting, other = _factory(), _factory()
    configured = other.db_url()
    adopting.adopt_login(_ADOPTED)
    assert other.db_url() == configured
