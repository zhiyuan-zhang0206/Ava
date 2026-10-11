"""Machine SDK reads require a context, without requiring an agent or plugins."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

import ava
import ava.self as self_mod
from base.agents.context import AvaContext
from base.cluster.machine import MachineIdentity, MachineNameMissing


@pytest.fixture
def restore_context() -> Iterator[None]:
    prior = ava.unbind_context()
    if prior is not None:
        ava.bind_context(prior)
    try:
        yield
    finally:
        ava.unbind_context()
        if prior is not None:
            ava.bind_context(prior)


def _machine(name: str, description: str | None) -> MachineIdentity:
    return MachineIdentity(
        name=lambda: name,
        role=lambda: frozenset({"gateway"}),
        host=lambda: "localhost",
        description=lambda: description,
    )


@pytest.mark.usefixtures("restore_context")
def test_machine_spec_tuple_with_description() -> None:
    ava.bind_context(AvaContext(machine=_machine("test-host", "voice IO + browser")))
    assert self_mod.MACHINE_SPEC == ("test-host", "voice IO + browser")
    assert self_mod.SELF_MACHINE_NAME == "test-host"


@pytest.mark.usefixtures("restore_context")
def test_machine_spec_tuple_empty_when_no_description() -> None:
    ava.bind_context(AvaContext(machine=_machine("wsl", None)))
    assert self_mod.MACHINE_SPEC == ("wsl", "")


@pytest.mark.usefixtures("restore_context")
def test_machine_reads_require_a_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    ava.unbind_context()
    for field in ("MACHINE_SPEC", "SELF_MACHINE_NAME"):
        with pytest.raises(AttributeError, match="context"):
            getattr(self_mod, field)


@pytest.mark.usefixtures("restore_context")
def test_partial_context_has_no_machine_fallback() -> None:
    ava.bind_context(AvaContext())
    with pytest.raises(RuntimeError, match="no MachineIdentity"):
        _ = self_mod.SELF_MACHINE_NAME


@pytest.mark.usefixtures("restore_context")
def test_no_agent_machine_owner_stays_lazy_and_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[str] = []

    def name() -> str:
        reads.append("name")
        return "explicit"

    def description() -> str:
        reads.append("description")
        return "owned"

    def forbidden() -> object:
        pytest.fail("a machine read must not resolve clients or plugins")

    monkeypatch.setattr(ava, "ensure_plugins_loaded", forbidden)
    machine = MachineIdentity(
        name=name,
        description=description,
        role=forbidden,  # type: ignore[arg-type]  # deliberate failing reader
        host=forbidden,  # type: ignore[arg-type]  # deliberate failing reader
    )
    context = AvaContext(machine=machine)
    ava.bind_context(context)
    assert reads == []
    assert self_mod.SELF_MACHINE_NAME == "explicit"
    assert self_mod.SELF_MACHINE_NAME == "explicit"
    assert reads == ["name"]
    assert self_mod.MACHINE_SPEC == ("explicit", "owned")
    assert reads == ["name", "description"]
    assert context.identity is None
    assert context.db is None
    ava.bind_context(replace(context, machine=_machine("other", None)))
    assert self_mod.SELF_MACHINE_NAME == "other"
    assert machine.name() == "explicit"


@pytest.mark.usefixtures("restore_context")
def test_owned_machine_preserves_missing_name_error() -> None:
    ava.bind_context(AvaContext(machine=_machine("", None)))
    with pytest.raises(MachineNameMissing):
        _ = self_mod.MACHINE_SPEC
