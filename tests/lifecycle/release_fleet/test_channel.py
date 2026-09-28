"""The coordinator channel over a real loopback listener: authentication, replay and routing.

The listener and the unit client are real (stdlib HTTP on 127.0.0.1); the
enrollment is the gateway store's own record (`ensure_enrollment`), rotated
or revoked through the real operator functions.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from cli.release_fleet import listener as listener_module
from cli.release_fleet.client import (
    CapabilityDeferredError,
    CoordinatorAwayError,
    CoordinatorClient,
    StaleReportError,
)
from cli.release_fleet.listener import CoordinatorListener, proof_headers, route
from cli.release_fleet.policy import UnitKey
from cli.release_fleet.progress import Instruction, Report
from cli.release_fleet.request import CoordinatorEndpoint
from shared.cluster.authority.channel import ChannelRefusedError, sign_request
from shared.cluster.authority.unit import (
    Enrollment,
    UnitIdentity,
    ensure_enrollment,
    revoke_enrollment,
    rotate_enrollment,
)

_RUNNER = UnitKey(machine="macbook-air", home="/Users/zzy/.ava")
_OTHER = UnitKey(machine="company-mini", home="/Users/zhiyuan-output/.ava")
_WHEN = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


class Channel:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.operation = uuid4()
        self.listener = CoordinatorListener(self.operation, home, (_RUNNER, _OTHER))
        host, port = self.listener.start("127.0.0.1", 0)
        self.endpoint = CoordinatorEndpoint(host=host, port=port)
        self.enrollment = ensure_enrollment(home, _identity(_RUNNER))

    def client(self, enrollment: Enrollment | None = None, **changes: object) -> CoordinatorClient:
        fields: dict[str, object] = {
            "endpoint": self.endpoint,
            "operation": self.operation,
            "unit": _RUNNER,
            "enrollment": enrollment or self.enrollment,
        } | changes
        return CoordinatorClient(**fields)  # type: ignore[arg-type]

    def instruction(self, **changes: object) -> Instruction:
        fields: dict[str, object] = {
            "operation": self.operation,
            "unit": _RUNNER,
            "sequence": 1,
            "action": "quiesce",
            "direction": "candidate",
            "image": ("a" * 64, "b" * 64),
            "maintenance_at": _WHEN,
        } | changes
        return Instruction.model_validate(fields)

    def report(self, instruction: Instruction, **changes: object) -> Report:
        fields: dict[str, object] = {
            "operation": self.operation,
            "unit": _RUNNER,
            "instruction": instruction.digest,
            "state": "closed",
            "at": _WHEN,
        } | changes
        return Report.model_validate(fields)


def _identity(unit: UnitKey) -> UnitIdentity:
    return UnitIdentity(machine=unit.machine, home=unit.home)


@pytest.fixture
def channel(tmp_path: Path) -> Iterator[Channel]:
    home = tmp_path.resolve() / "gateway"
    home.mkdir(mode=0o700)
    served = Channel(home)
    try:
        yield served
    finally:
        served.listener.close()


def test_a_unit_pulls_its_journaled_instruction_and_its_answer_is_queued(
    channel: Channel,
) -> None:
    client = channel.client()
    assert client.instruction() is None  # nothing issued yet
    instruction = channel.instruction(action="close")
    channel.listener.publish([instruction])
    assert client.instruction() == instruction
    report = channel.report(instruction)
    client.report(report)
    assert channel.listener.reports.get_nowait() == report


def test_an_answer_to_a_replaced_instruction_is_stale(channel: Channel) -> None:
    first = channel.instruction()
    channel.listener.publish([first.model_copy(update={"sequence": 2})])
    with pytest.raises(StaleReportError, match="answers no current instruction"):
        channel.client().report(channel.report(first))
    assert channel.listener.reports.empty()


def test_a_report_naming_another_unit_is_refused(channel: Channel) -> None:
    instruction = channel.instruction()
    channel.listener.publish([instruction])
    foreign = channel.report(instruction).model_copy(update={"unit": _OTHER})
    with pytest.raises(ChannelRefusedError, match="another operation or unit"):
        channel.client().report(foreign)
    assert channel.listener.reports.empty()


def test_a_rotated_or_revoked_enrollment_stops_authenticating_at_once(channel: Channel) -> None:
    captured = channel.client()
    rotate_enrollment(channel.home, _identity(_RUNNER))
    with pytest.raises(ChannelRefusedError, match="does not authenticate"):
        captured.instruction()
    rotated = channel.client(enrollment=ensure_enrollment(channel.home, _identity(_RUNNER)))
    assert rotated.instruction() is None
    revoke_enrollment(channel.home, _identity(_RUNNER))
    with pytest.raises(ChannelRefusedError, match="does not authenticate"):
        rotated.instruction()


def test_a_forged_signature_never_burns_the_nonce(channel: Channel) -> None:
    path = route(channel.operation, _RUNNER)
    proof = sign_request(
        channel.enrollment, operation=str(channel.operation), method="GET", path=path, body=b""
    )
    forged = proof_headers(proof) | {"X-Ava-Signature": "0" * 64}
    assert _status(channel, path, forged) == 401
    assert _status(channel, path, proof_headers(proof)) == 204
    # The admitted nonce is spent: the exact same request replayed is refused.
    assert _status(channel, path, proof_headers(proof)) == 401


def test_a_request_outside_the_clock_skew_is_refused(channel: Channel) -> None:
    path = route(channel.operation, _RUNNER)
    stale = sign_request(
        channel.enrollment,
        operation=str(channel.operation),
        method="GET",
        path=path,
        body=b"",
        now=time.time() - 3600,
    )
    assert _status(channel, path, proof_headers(stale)) == 401


def test_another_operation_or_unit_is_not_served(channel: Channel) -> None:
    with pytest.raises(ChannelRefusedError, match="does not authenticate"):
        channel.client(operation=uuid4()).instruction()
    stranger = UnitKey(machine="win", home="C:\\Users\\zzy\\.ava")
    enrolled = ensure_enrollment(channel.home, _identity(stranger))
    with pytest.raises(ChannelRefusedError, match="does not authenticate"):
        channel.client(enrollment=enrolled, unit=stranger).instruction()
    with pytest.raises(ChannelRefusedError, match="another unit"):
        channel.client(unit=_OTHER)


def test_every_unauthenticated_request_gets_one_answer(channel: Channel) -> None:
    """A peer without a proof cannot tell a wrong operation, a unit that takes no
    part, a unit without an enrollment and a failing proof apart."""
    path = route(channel.operation, _RUNNER)

    def signed(target: str, now: float | None = None) -> dict[str, str]:
        operation = str(channel.operation)
        return proof_headers(
            sign_request(
                channel.enrollment,
                operation=operation,
                method="GET",
                path=target,
                body=b"",
                now=now,
            )
        )

    spent = signed(path)
    assert _answer(channel, path, spent)[0] == 204
    stranger = route(channel.operation, UnitKey(machine="win", home="C:\\Users\\zzy\\.ava"))
    unenrolled = route(channel.operation, _OTHER)
    another_operation = route(uuid4(), _RUNNER)
    answers = {
        "no such route": _answer(channel, "/v1/nothing", signed("/v1/nothing")),
        "another operation": _answer(channel, another_operation, signed(another_operation)),
        "no part in the operation": _answer(channel, stranger, signed(stranger)),
        "no enrollment": _answer(channel, unenrolled, signed(unenrolled)),
        "no proof": _answer(channel, path, {}),
        "forged proof": _answer(channel, path, signed(path) | {"X-Ava-Signature": "0" * 64}),
        "replayed proof": _answer(channel, path, spent),
        "stale proof": _answer(channel, path, signed(path, now=time.time() - 3600)),
    }
    uniform = (401, b'{"error": "the coordinator request does not authenticate"}')
    assert answers == dict.fromkeys(answers, uniform)


def test_the_capability_exchange_is_deferred_to_dbgen8(channel: Channel) -> None:
    with pytest.raises(CapabilityDeferredError, match="dbgen-8"):
        channel.client().capability()


def test_an_oversized_body_is_refused_before_it_is_read(channel: Channel) -> None:
    path = route(channel.operation, _RUNNER, "/report")
    body = b"x" * (64 * 1024 + 1)
    proof = sign_request(
        channel.enrollment, operation=str(channel.operation), method="POST", path=path, body=body
    )
    assert _status(channel, path, proof_headers(proof), body=body) == 413


def test_a_closed_listener_is_away_not_refusing(channel: Channel) -> None:
    client = channel.client(timeout_s=2.0)
    channel.listener.close()
    with pytest.raises(CoordinatorAwayError):
        client.instruction()


def _answer(channel: Channel, path: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """A GET's status and body."""
    request = urllib.request.Request(  # noqa: S310 — the test's own loopback listener
        channel.endpoint.url + path, headers=headers, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as answer:  # noqa: S310 — same URL
            return answer.status, answer.read()
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read()


def _status(channel: Channel, path: str, headers: dict[str, str], body: bytes | None = None) -> int:
    request = urllib.request.Request(  # noqa: S310 — the test's own loopback listener
        channel.endpoint.url + path,
        data=body,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as answer:  # noqa: S310 — same URL
            return answer.status
    except urllib.error.HTTPError as refused:
        return refused.code


def _raw(channel: Channel, head: str, body: bytes = b"", *, finish: bool = False) -> bytes:
    """Send `head` (and `body`) on a raw connection; the first answer bytes.

    Without `finish` the connection stays open for writing, so a listener that
    waits for more body instead of answering fails the 5 s read below.
    """
    with socket.create_connection((channel.endpoint.host, channel.endpoint.port)) as raw:
        raw.settimeout(5)
        raw.sendall(head.encode() + b"\r\n\r\n" + body)
        if finish:
            raw.shutdown(socket.SHUT_WR)
        return raw.recv(4096)


@pytest.mark.parametrize(
    "length",
    [
        "Content-Length: -1",
        "Content-Length: abc",
        "Content-Length: 1.5",
        "Content-Length: +1",
        "Content-Length: 0x10",
        "Content-Length: 2\r\nContent-Length: 2",
    ],
)
def test_a_malformed_content_length_answers_400_without_reading(
    channel: Channel, length: str
) -> None:
    path = route(channel.operation, _RUNNER, "/report")
    answer = _raw(channel, f"POST {path} HTTP/1.1\r\nHost: x\r\n{length}", b"x" * 4096)
    assert answer.startswith(b"HTTP/1.0 400 "), answer


@pytest.mark.parametrize(
    "length", [str(64 * 1024 + 1), "9" * 5000], ids=["one-over-the-cap", "5000-digits"]
)
def test_an_oversized_content_length_answers_413_without_reading(
    channel: Channel, length: str
) -> None:
    path = route(channel.operation, _RUNNER, "/report")
    answer = _raw(channel, f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {length}", b"x")
    assert answer.startswith(b"HTTP/1.0 413 "), answer


@pytest.fixture
def bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Channel]:
    """A listener with a 0.5 s read timeout and room for two requests at once."""
    monkeypatch.setattr(listener_module, "READ_TIMEOUT_S", 0.5)
    monkeypatch.setattr(listener_module, "MAX_CONCURRENT_REQUESTS", 2)
    home = tmp_path.resolve() / "bounded"
    home.mkdir(mode=0o700)
    served = Channel(home)
    try:
        yield served
    finally:
        served.listener.close()


def _stall(channel: Channel) -> socket.socket:
    """A connection that sent half a request line and nothing more."""
    raw = socket.create_connection((channel.endpoint.host, channel.endpoint.port))
    raw.settimeout(5)
    raw.sendall(b"GET /v1/op/")
    return raw


def _closed_unanswered(raw: socket.socket) -> bool:
    try:
        return raw.recv(4096) == b""
    except ConnectionResetError:
        return True


def test_a_stalled_connection_is_dropped_after_the_read_timeout(bounded: Channel) -> None:
    with _stall(bounded) as raw:
        assert _closed_unanswered(raw)  # within the 0.5 s timeout, not the test's 5 s


def test_connections_beyond_the_cap_are_closed_unanswered(bounded: Channel) -> None:
    path = route(bounded.operation, _RUNNER)
    request = f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
    address = (bounded.endpoint.host, bounded.endpoint.port)
    with _stall(bounded), _stall(bounded), socket.create_connection(address) as third:
        third.settimeout(5)
        third.sendall(request)
        assert _closed_unanswered(third)
    # The stalled requests end and free their slots: a unit is served again.
    deadline = time.monotonic() + 5
    while True:
        try:
            assert bounded.client().instruction() is None
            break
        except CoordinatorAwayError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
