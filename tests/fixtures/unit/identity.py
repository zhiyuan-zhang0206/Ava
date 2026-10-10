"""Explicit machine-role fixtures; callers opt in from their local conftest."""

import contextlib
from collections.abc import Generator, Iterator

import psycopg
import pytest


@contextlib.contextmanager
def _machine_identity(*, role: str, name: str | None = None) -> Generator[None]:
    """Switch this process's resolved machine identity, restoring on exit.

    Injects via base.cluster.machine.set_identity so every `from base.cluster.machine import
    machine_role` / `machine_name` call site sees the new value without
    per-module patching. `name=None` leaves machine_name as-is — no injection; it
    resolves lazily from settings if not yet cached, otherwise
    returns the already-cached value. The finally block resets the holder, so the
    session default is restored — no per-field save/restore is needed because the
    holder re-resolves lazily after reset.
    """
    from base.cluster.machine import reset_identity, set_identity

    if name is None:
        set_identity(role=role)  # pyright: ignore[reportArgumentType]  # str passthrough to MachineRole literal
    else:
        set_identity(role=role, name=name)  # pyright: ignore[reportArgumentType]
    try:
        yield
    finally:
        reset_identity()


@pytest.fixture
def set_machine_identity() -> Iterator[object]:
    """Factory: switch machine role (and optionally name) at the source via
    base.cluster.machine.set_identity.

        def test_x(set_machine_identity, db_conn):
            set_machine_identity(role="gateway", name="cloud-test")

    May be called more than once within a test to flip roles; the last call
    wins, and the holder is reset at teardown (restoring the session default).
    """
    from base.cluster.machine import reset_identity, set_identity

    def _set(role: str, name: str | None = None) -> None:
        if name is None:
            set_identity(role=role)  # pyright: ignore[reportArgumentType]
        else:
            set_identity(role=role, name=name)  # pyright: ignore[reportArgumentType]

    try:
        yield _set
    finally:
        reset_identity()


@pytest.fixture
def runner_unit(db_conn: psycopg.Connection) -> Iterator[None]:
    """The agent-runner unit (agent-runner): role='agent-runner', holds a
    gateway_url pointing at the gateway, no local gateway/docker.

    This is the SDK side — spawn_agent takes the local-spawn path here. Matches
    the session default role, but as an explicit opt-in so a test declares it is
    the runner unit rather than relying on the global default.
    """
    _ = db_conn  # per-test truncate side effect
    with _machine_identity(role="agent-runner"):
        yield
