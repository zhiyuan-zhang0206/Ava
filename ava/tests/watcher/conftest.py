"""Consumer-local opt-in fixtures for this test directory."""

import dataclasses
from collections.abc import Callable, Collection

import pytest

from ava.shell import sessions as real_sessions
from ava.watcher import plans, spawn, wiring
from tests.fixtures.model_catalog import add_bindings as add_bindings
from tests.fixtures.model_catalog import add_models as add_models
from tests.fixtures.model_catalog import model_catalog as model_catalog
from tests.fixtures.model_catalog import set_prices as set_prices
from tests.fixtures.unit.config_authority import config_authority as config_authority
from tests.fixtures.unit.model_owner import model_installation as model_installation
from tests.fixtures.unit.model_owner import sdk_model_owner as sdk_model_owner

type Send = Callable[[int, str], None]
type CreateSession = Callable[..., tuple[int, str]]


class _Sessions:
    """The real shell sessions, with `send` and `create_session` optionally replaced."""

    def __init__(self, *, send: Send | None, create_session: CreateSession | None) -> None:
        self._send = send
        self._create_session = create_session

    def create_session(
        self, name: str, *, ttl: float, system: bool, env_overrides: dict[str, str]
    ) -> tuple[int, str]:
        create = self._create_session or real_sessions.create_session
        return create(name, ttl=ttl, system=system, env_overrides=env_overrides)

    def send(self, session_id: int, line: str, /) -> None:
        (self._send or real_sessions.send)(session_id, line)

    def kill(self, session_id: int, /) -> None:
        real_sessions.kill(session_id)


@pytest.fixture
def launch_watcher() -> Callable[..., int]:
    """`ava.watcher.launch` with some shell-session calls replaced, without patching the module.

    `wiring.spawn_deps()` builds the real deps; the returned callable swaps the session calls a
    test needs to observe or fake and runs the same plan -> `spawn.run` path as the facade.
    """

    def launch(
        code: str,
        timeout: float | str,
        name: str,
        *,
        send: Send | None = None,
        create_session: CreateSession | None = None,
        live_session_ids: Callable[[], Collection[int] | None] | None = None,
    ) -> int:
        deps = wiring.spawn_deps()
        deps = dataclasses.replace(
            deps,
            sessions=_Sessions(send=send, create_session=create_session),
            live_session_ids=live_session_ids or deps.live_session_ids,
        )
        return spawn.run(plans.plan_launch(code, timeout, name, None), deps)

    return launch
