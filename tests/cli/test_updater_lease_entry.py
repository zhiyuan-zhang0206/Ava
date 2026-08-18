"""`cli.commands._updater_lease` — the shell-chain seam for the updater lease (R1).

The update legs are shell chains that cannot import Python state; this module
is their parameter-translation seam into `shared.host_deploy_state`. Tests pin
the two verbs (touch = arm a live lease + enter converging; clear = drop the
lease, posture untouched), the unknown-verb refusal, and that the module is
invokable as `python -m cli.commands._updater_lease` (the exact invocation the
POSIX / cmd.exe chains run).
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from shared import host_deploy_state as hds


@pytest.fixture(autouse=True)
def _clean_row(db_conn: psycopg.Connection) -> Iterator[None]:
    """host_deploy_state is not in the conftest TRUNCATE list — self-manage."""
    from shared.machine import machine_name

    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM host_deploy_state WHERE machine = %s", (machine_name(),))
    db_conn.commit()
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM host_deploy_state WHERE machine = %s", (machine_name(),))
    db_conn.commit()


def _run_cli(verb: str) -> int:
    from cli.commands import _updater_lease

    return _updater_lease._main(["_updater_lease", verb])


def test_touch_arms_a_live_lease_and_enters_converging() -> None:
    assert _run_cli("touch") == 0
    state = hds.read()
    assert state is not None
    assert state.posture == "converging"
    assert state.updater_live is True


def test_clear_drops_the_lease_and_leaves_posture_alone() -> None:
    _run_cli("touch")
    # A completed update returns the host to idle through `ava start`'s tail; the
    # chain's tail clear must not stamp it back to converging.
    hds.set_posture("idle")
    assert _run_cli("clear") == 0
    state = hds.read()
    assert state is not None
    assert state.updater_live is False
    assert state.posture == "idle"


def test_clear_without_touch_is_a_noop() -> None:
    assert _run_cli("clear") == 0
    assert hds.read() is None


def test_unknown_verb_is_refused() -> None:
    assert _run_cli("bogus") == 1
