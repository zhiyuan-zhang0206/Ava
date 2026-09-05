"""Every production Postgres connect carries `shared.db.PG_KEEPALIVE_KWARGS`, so
a database that **black-holes** packets fails fast instead of parking the caller
on the OS TCP-retransmit timeout.

The dangerous shape is not a refused connection (`ECONNREFUSED` arrives
immediately and every call site already handles it) but a peer that completes the
TCP handshake and then never speaks — a runner that changed networks, a stale
route, a firewall that drops rather than rejects. libpq has no application-level
bound on that wait unless `connect_timeout` is set, so
`shared.migrations.assert_schema_current` — the FIRST thing a daemon does at boot
— used to hang there forever. The supervisor's `respawn_and_verify` then polls
for 20s, reports the daemon down, and respawns another one that wedges the same
way: bounded churn, but the diagnosis reads "failed to start" when the process is
really blocked on a socket.

`silent_peer_url` reproduces exactly that shape locally (no network egress, no
DB): a listening socket that accepts and never writes a byte.
`test_a_bare_connect_hangs_on_a_silent_peer` is the control — it pins that the
fixture really is a black hole, so the fail-fast assertions below measure the
timeout and not an error the OS would have delivered anyway.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Callable, Generator
from typing import Any

import psycopg
import pytest

from shared.config import settings
from shared.db import PG_KEEPALIVE_KWARGS

# The control test waits this long and asserts a bare connect is STILL pending;
# the fail-fast tests assert they finished inside it. Comfortably above
# PG_KEEPALIVE_KWARGS' 5s connect_timeout (libpq enforces it itself — measured
# ~5.1s), so a loaded CI runner has margin without the bound going vacuous.
_HANG_WINDOW_S = 8.0


@pytest.fixture
def silent_peer_url() -> Generator[str]:
    """A `postgresql://` URL for a TCP peer that accepts and then never speaks.

    The kernel completes the handshake from the listen backlog, so libpq gets a
    connected socket, sends its startup packet, and waits for a reply that never
    comes — no error is ever delivered. Accepted sockets are held (never read,
    never closed) for the test's lifetime and closed on teardown, which lets any
    still-waiting client thread error out instead of leaking.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    held: list[socket.socket] = []

    def accept_and_go_silent() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:  # server socket closed by teardown
                return
            held.append(conn)

    threading.Thread(target=accept_and_go_silent, daemon=True).start()
    try:
        yield f"postgresql://ava:secret@127.0.0.1:{port}/ava"
    finally:
        srv.close()
        for conn in held:
            conn.close()


def test_a_bare_connect_hangs_on_a_silent_peer(silent_peer_url: str) -> None:
    """The control. Without `connect_timeout` libpq is still waiting after
    `_HANG_WINDOW_S` — the fixture is a black hole, not a refusal, so every
    "finished inside the window" assertion below is a real fail-fast measurement.
    """
    finished = threading.Event()

    def bare_connect() -> None:
        # Suppressed deliberately: this test asserts only on whether the connect
        # RETURNED, never on how. Any outcome other than "still blocked" fails it.
        with contextlib.suppress(Exception):
            psycopg.connect(silent_peer_url).close()
        finished.set()

    # Daemon thread: it is expected to still be blocked when the test ends, and
    # the fixture teardown closes the peer socket to release it.
    threading.Thread(target=bare_connect, daemon=True).start()
    assert not finished.wait(_HANG_WINDOW_S), (
        "a bare psycopg.connect() returned against the silent peer — the fixture "
        "is no longer reproducing a black hole, so the fail-fast tests below "
        "would pass vacuously"
    )


def _assert_fails_fast(attempt: Callable[[], object]) -> None:
    """Assert `attempt()` raises `psycopg.OperationalError` within `_HANG_WINDOW_S`.

    Runs in a worker thread rather than inline so that a **regression** — the
    resilience kwargs dropped from a call site again — surfaces as a clean
    assertion failure instead of hanging the suite until CI's global timeout. The
    defect under test is precisely an unbounded wait, so an inline call could not
    report on itself.
    """
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            attempt()
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(_HANG_WINDOW_S)
    elapsed = time.monotonic() - started
    assert not worker.is_alive(), (
        f"still connecting after {elapsed:.1f}s — this connect is not bounded by "
        "connect_timeout, so a black-holing database hangs the caller"
    )
    assert isinstance(outcome[0], psycopg.OperationalError), (
        f"expected the connect to raise OperationalError, got {outcome[0]!r}"
    )


def test_resilience_kwargs_bound_the_connect(silent_peer_url: str) -> None:
    """`PG_KEEPALIVE_KWARGS` itself is what converts the hang into an error —
    the property every call site below inherits by passing that one constant."""
    _assert_fails_fast(lambda: psycopg.connect(silent_peer_url, **PG_KEEPALIVE_KWARGS))


def test_assert_schema_current_fails_fast(silent_peer_url: str) -> None:
    """The reported defect: a daemon's boot-time schema assertion raises instead
    of wedging the whole startup on a black-holed database."""
    from shared.migrations import assert_schema_current

    _assert_fails_fast(lambda: assert_schema_current(silent_peer_url))


def test_ava_db_connect_fails_fast(silent_peer_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava.DB` is dialled from inside the agent's exec sandbox — an unbounded
    connect freezes the agent's tool call, not just a daemon's boot."""
    from ava._settings import _connect_db

    monkeypatch.setattr(settings.data_plane, "db_url", silent_peer_url)
    _assert_fails_fast(_connect_db)  # pyright: ignore[reportUnknownArgumentType]


def _record_connect_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace `psycopg.connect` with a spy that records its kwargs and raises.

    Used for the shell-session call site whose *behaviour* is impractical to
    drive here because allocation needs an agent identity in agents_meta. The
    behaviour is already proven above — this pins that the site hands libpq the
    same constant, which is the whole mechanism.
    """
    seen: dict[str, Any] = {}

    def spy(_conninfo: str = "", **kwargs: Any) -> psycopg.Connection:
        seen.update(kwargs)
        raise psycopg.OperationalError("spy: connection not attempted")

    monkeypatch.setattr(psycopg, "connect", spy)
    return seen


def test_shell_session_index_passes_the_resilience_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configuration pin, not a behavioural assertion: `ava.shell.new()`'s
    session-index allocation hands libpq `PG_KEEPALIVE_KWARGS`."""
    import ava._boot
    from ava.shell import sessions

    monkeypatch.setattr(ava._boot, "agent_id", lambda: 1)
    seen = _record_connect_kwargs(monkeypatch)
    with pytest.raises(psycopg.OperationalError):
        sessions._next_session_index_from_db()
    assert seen.items() >= PG_KEEPALIVE_KWARGS.items()
