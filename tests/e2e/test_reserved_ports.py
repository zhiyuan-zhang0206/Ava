import contextlib
import errno
import socket
from collections.abc import Iterator

import pytest

from tests.e2e._ports import (
    RESERVED_PORT_BASE,
    RESERVED_PORT_STRIDE,
    ephemeral_port_floor,
    reserve_port,
)

_BASE = 28000
_STRIDE = 8
_ATTEMPTS = 4


def _reserve(slot: int):
    return reserve_port(slot, base=_BASE, stride=_STRIDE, attempts=_ATTEMPTS)


def _node_like_listen(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen()
    except OSError:
        sock.close()
        raise
    return sock


@pytest.fixture
def thieves() -> Iterator[list[socket.socket]]:
    held: list[socket.socket] = []
    yield held
    for sock in held:
        sock.close()


def test_a_held_port_cannot_be_taken_by_a_node_like_listener_until_it_is_released() -> None:
    reservation = _reserve(1)
    try:
        with pytest.raises(OSError) as taken:
            _node_like_listen(reservation.port)
        assert taken.value.errno == errno.EADDRINUSE
    finally:
        reservation.release()
    with contextlib.closing(_node_like_listen(reservation.port)) as listener:
        assert listener.getsockname()[1] == reservation.port
    reservation.release()


def test_a_port_stolen_before_the_pick_is_skipped_not_returned(
    thieves: list[socket.socket],
) -> None:
    first_candidate = _BASE + 2
    thieves.append(_node_like_listen(first_candidate))
    reservation = _reserve(2)
    try:
        assert reservation.port == first_candidate + _STRIDE
        with pytest.raises(OSError):
            _node_like_listen(reservation.port)
    finally:
        reservation.release()


def test_every_candidate_taken_is_a_loud_failure(thieves: list[socket.socket]) -> None:
    for attempt in range(_ATTEMPTS):
        thieves.append(_node_like_listen(_BASE + attempt * _STRIDE + 3))
    with pytest.raises(RuntimeError, match="every reserved port candidate for slot 3 is in use"):
        _reserve(3)


def test_workers_in_different_slots_never_share_a_port_even_when_held_together() -> None:
    reservations = [_reserve(slot) for slot in range(_STRIDE)]
    try:
        ports = [reservation.port for reservation in reservations]
        assert len(set(ports)) == len(ports)
    finally:
        for reservation in reservations:
            reservation.release()


def test_two_holders_of_the_same_slot_take_different_candidates() -> None:
    first, second = _reserve(4), _reserve(4)
    try:
        assert second.port == first.port + _STRIDE
    finally:
        first.release()
        second.release()


def test_the_default_block_is_below_the_kernel_ephemeral_range() -> None:
    top = RESERVED_PORT_BASE + 7 * RESERVED_PORT_STRIDE + RESERVED_PORT_STRIDE
    assert top < ephemeral_port_floor()


def test_a_block_inside_the_ephemeral_range_is_refused() -> None:
    with pytest.raises(RuntimeError, match="reaches the kernel's ephemeral range"):
        reserve_port(0, base=ephemeral_port_floor(), stride=8, attempts=2)


def test_a_slot_outside_the_stride_is_refused() -> None:
    with pytest.raises(ValueError, match="outside"):
        _reserve(_STRIDE)
