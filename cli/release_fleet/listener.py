"""The coordinator listener: remote unit executors pull instructions and report.

It lives exactly as long as one coordinator run of one operation, bound on the
gateway's reachable address at the home's reserved `coordinator` port, and it
works while the gateway application, its bearer and the pooler are down: it
reads nothing but the journaled instructions the coordinator hands it and the
gateway's enrollment store. Routes, under `/v1/op/<operation>/unit/<unit key>`
(the unit key is `UnitIdentity.key`):

- `GET` answers the unit's current instruction (`204` before the first);
- `POST .../report` takes a `Report` answering exactly that instruction and
  queues it (`202`); only the coordinator's own thread journals it;
- `POST .../capability` is the per-operation capability exchange, slice
  dbgen-8, and answers `501` naming it.

Every request authenticates with `shared.cluster.authority.channel`: an HMAC
proof keyed by the unit's enrollment secret, checked against the gateway's
current record (a rotated or revoked enrollment stops at once) inside one
replay window per run. A continuation starts a new window, so every route is
idempotent: a report names the instruction it answers.

Before any proof is checked, what an unauthenticated peer can cost is bounded:
a body is read only when one plain decimal `Content-Length` declares it
within `MAX_BODY_BYTES` (otherwise `400` or `413`, nothing read), a socket
read that waits `READ_TIMEOUT_S` drops the connection, so does a request
still unanswered `REQUEST_DEADLINE_S` after its accept (a peer trickling
bytes inside every read timeout), and at most `MAX_CONCURRENT_REQUESTS` are
served at once (a connection beyond them is closed unanswered). Every request
that does not authenticate (a wrong route, operation or unit, a missing
enrollment, a failing proof) gets one uniform `401`; the reason is only
logged on the coordinator.
"""

from __future__ import annotations

import io
import json
import queue
import re
import socket
import threading
import time
from collections.abc import Buffer, Callable, Iterable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from cli.release_fleet.policy import UnitKey
from cli.release_fleet.progress import Instruction, Report
from shared.cluster.authority.channel import (
    ChannelRefusedError,
    ReplayWindow,
    RequestProof,
    verify_request,
)
from shared.cluster.authority.unit import Enrollment, UnitIdentity, load_enrollment
from shared.deploy.progress_timeout import (
    COORDINATOR_READ_TIMEOUT_S,
    COORDINATOR_REQUEST_DEADLINE_S,
)
from shared.log import logger

MAX_BODY_BYTES = 64 * 1024
_DECIMAL = re.compile(r"[0-9]+")
# How long one socket read may wait, and the whole request from accept (the
# lattice's coordinator-channel clocks): a unit sends each request whole, so
# only a stalled or trickling peer reaches either, and its connection is dropped.
READ_TIMEOUT_S = COORDINATOR_READ_TIMEOUT_S
REQUEST_DEADLINE_S = COORDINATOR_REQUEST_DEADLINE_S
# Requests served at once. Each unit's follower holds at most one, so this is
# far above a fleet's need; a peer opening more is closed unanswered, which a
# unit reads as the coordinator being away and retries.
MAX_CONCURRENT_REQUESTS = 32
# The one answer to every request that does not authenticate, whatever the
# reason: a peer without a proof cannot tell a wrong operation, a unit that
# takes no part, a missing enrollment and a failing proof apart.
UNAUTHENTICATED = "the coordinator request does not authenticate"
_LOGGED_PATH = 256
CAPABILITY_REFUSAL = (
    "the per-operation capability exchange over the coordinator channel is slice dbgen-8"
)
# Proof headers; their names are part of the channel protocol `ava-coordinator/1`.
ENROLLMENT_HEADER = "X-Ava-Enrollment"
TIMESTAMP_HEADER = "X-Ava-Timestamp"
NONCE_HEADER = "X-Ava-Nonce"
SIGNATURE_HEADER = "X-Ava-Signature"
# What socketserver hands a request handler (a TCP server: the socket).
_Request = socket.socket | tuple[bytes, socket.socket]


def unit_key(unit: UnitKey) -> str:
    """The path segment naming a unit: the enrollment store's key for it."""
    return UnitIdentity(machine=unit.machine, home=unit.home).key


def route(operation: UUID, unit: UnitKey, suffix: str = "") -> str:
    return f"/v1/op/{operation}/unit/{unit_key(unit)}{suffix}"


def proof_headers(proof: RequestProof) -> dict[str, str]:
    return {
        ENROLLMENT_HEADER: proof.enrollment_id,
        TIMESTAMP_HEADER: str(proof.timestamp),
        NONCE_HEADER: proof.nonce,
        SIGNATURE_HEADER: proof.signature,
    }


class _Refused(Exception):  # noqa: N818 — an HTTP refusal carrying its status
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class _Unauthenticated(Exception):  # noqa: N818 — a refusal before authentication
    """Why a request did not authenticate: logged here, never told to the peer."""


def _body_length(declared: list[str]) -> int:
    """The body length one plain decimal `Content-Length` declares (none: 0).

    Checked before any body byte is read or any proof verified: a negative,
    signed, non-decimal or repeated length is a 400, one past the cap a 413.
    """
    if not declared:
        return 0
    if len(declared) > 1 or _DECIMAL.fullmatch(declared[0]) is None:
        raise _Refused(HTTPStatus.BAD_REQUEST, "the request declares no valid Content-Length")
    value = declared[0]
    if len(value) > len(str(MAX_BODY_BYTES)) or int(value) > MAX_BODY_BYTES:
        raise _Refused(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
    return int(value)


def _proof(headers: Mapping[str, str]) -> RequestProof:
    """The proof from `headers`, whose names are lowercased (HTTP names are caseless)."""
    try:
        return RequestProof(
            headers[ENROLLMENT_HEADER.lower()],
            int(headers[TIMESTAMP_HEADER.lower()]),
            headers[NONCE_HEADER.lower()],
            headers[SIGNATURE_HEADER.lower()],
        )
    except (KeyError, ValueError, ChannelRefusedError) as exc:
        raise _Unauthenticated("the request carries no channel proof") from exc


class CoordinatorListener:
    """One operation's listener: journaled instructions out, reports queued in.

    `units` are the operation's remote units; `home` is the gateway home whose
    enrollment store authenticates them. The coordinator publishes each unit's
    current instruction with `publish` (after journaling it) and drains
    `reports` from its own thread.
    """

    def __init__(
        self,
        operation: UUID,
        home: Path,
        units: tuple[UnitKey, ...],
        *,
        enrollment: Callable[[Path, UnitIdentity], Enrollment | None] = load_enrollment,
    ) -> None:
        self.operation = operation
        self.home = home
        self._units = {unit_key(unit): unit for unit in units}
        self._enrollment = enrollment
        self._window = ReplayWindow(operation=str(operation))
        self._instructions: dict[UnitKey, Instruction] = {}
        self._lock = threading.Lock()
        self.reports: queue.Queue[Report] = queue.Queue()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self, host: str, port: int) -> tuple[str, int]:
        """Bind and serve in a daemon thread; the bound address."""
        self._server = _BoundedServer((host, port), _handler(self), MAX_CONCURRENT_REQUESTS)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fleet-listener", daemon=True
        )
        self._thread.start()
        bound = self._server.server_address
        return str(bound[0]), int(bound[1])

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join()

    def publish(self, instructions: Iterable[Instruction]) -> None:
        """Serve these journaled instructions (each to its own unit) from now on."""
        with self._lock:
            for instruction in instructions:
                self._instructions[instruction.unit] = instruction

    # ── requests ────────────────────────────────────────────────────────────

    def _authenticated(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> UnitKey:
        parts = path.split("/")
        # "", "v1", "op", <operation>, "unit", <key>[, <verb>]
        if len(parts) not in {6, 7} or parts[1:3] != ["v1", "op"] or parts[4] != "unit":
            raise _Unauthenticated("no such coordinator route")
        if parts[3] != str(self.operation):
            raise _Unauthenticated("the listener serves another operation")
        unit = self._units.get(parts[5])
        if unit is None:
            raise _Unauthenticated("the unit takes no part in this operation")
        proof = _proof(headers)
        enrollment = self._enrollment(self.home, UnitIdentity(machine=unit.machine, home=unit.home))
        if enrollment is None:
            raise _Unauthenticated("the unit holds no enrollment on this gateway")
        try:
            verify_request(
                enrollment, proof, window=self._window, method=method, path=path, body=body
            )
        except ChannelRefusedError as exc:
            raise _Unauthenticated(str(exc)) from exc
        return unit

    def handle(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[HTTPStatus, dict[str, object] | None]:
        """One request's status and JSON body; refusals never change state.

        `headers` are keyed by lowercased name. Every request that does not
        authenticate gets the same `401`: its reason is only logged here.
        """
        try:
            return self._route(method, path, headers, body)
        except _Unauthenticated as refused:
            logger.info(
                "[release-fleet] listener refused {} {}: {}", method, path[:_LOGGED_PATH], refused
            )
            return HTTPStatus.UNAUTHORIZED, {"error": UNAUTHENTICATED}
        except _Refused as refused:
            return refused.status, {"error": str(refused)}

    def _route(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[HTTPStatus, dict[str, object] | None]:
        unit = self._authenticated(method, path, headers, body)
        verb = path.split("/")[6] if path.count("/") == 6 else ""
        if (method, verb) == ("GET", ""):
            return self._instruction(unit)
        if (method, verb) == ("POST", "report"):
            return self._report(unit, body)
        if (method, verb) == ("POST", "capability"):
            return HTTPStatus.NOT_IMPLEMENTED, {"error": CAPABILITY_REFUSAL}
        raise _Refused(HTTPStatus.NOT_FOUND, "no such coordinator route")

    def _instruction(self, unit: UnitKey) -> tuple[HTTPStatus, dict[str, object] | None]:
        with self._lock:
            current = self._instructions.get(unit)
        if current is None:
            return HTTPStatus.NO_CONTENT, None
        return HTTPStatus.OK, json.loads(current.model_dump_json())

    def _report(self, unit: UnitKey, body: bytes) -> tuple[HTTPStatus, dict[str, object] | None]:
        try:
            report = Report.model_validate_json(body)
        except ValidationError as exc:
            raise _Refused(HTTPStatus.BAD_REQUEST, f"not a unit report: {exc}") from exc
        with self._lock:
            current = self._instructions.get(unit)
        if report.operation != self.operation or report.unit != unit:
            raise _Refused(HTTPStatus.FORBIDDEN, "the report belongs to another operation or unit")
        if current is None or report.instruction != current.digest:
            raise _Refused(HTTPStatus.CONFLICT, "the report answers no current instruction")
        self.reports.put(report)
        return HTTPStatus.ACCEPTED, {"accepted": report.instruction}


class _BoundedServer(ThreadingHTTPServer):
    """One daemon thread per request, at most `slots` at once; a connection
    beyond them is closed unanswered."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        slots: int,
    ) -> None:
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(slots)

    def process_request(self, request: _Request, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: _Request, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _DeadlineReader(io.RawIOBase):
    """A request's socket reads: each waits at most the read timeout and never
    past the request's deadline, where it raises `TimeoutError`, which the
    stdlib handler answers by dropping the connection."""

    def __init__(self, connection: socket.socket, deadline: float) -> None:
        self._connection = connection
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the coordinator request deadline passed")
        self._connection.settimeout(min(READ_TIMEOUT_S, remaining))
        return self._connection.recv_into(buffer)


def _handler(listener: CoordinatorListener) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ava-coordinator/1"
        timeout = READ_TIMEOUT_S

        def setup(self) -> None:
            """Start the request's deadline as the connection is taken from accept."""
            super().setup()
            self.rfile.close()
            deadline = time.monotonic() + REQUEST_DEADLINE_S
            self.rfile = io.BufferedReader(_DeadlineReader(self.connection, deadline))

        def version_string(self) -> str:
            """The protocol alone: the stdlib default appends the Python version."""
            return self.server_version

        def log_message(self, format: str, *args: object) -> None:
            logger.debug("[release-fleet] listener {}", format % args)

        def _serve(self, method: str) -> None:
            try:
                length = _body_length(self.headers.get_all("Content-Length") or [])
            except _Refused as refused:
                self._answer(refused.status, {"error": str(refused)})
                return
            body = self.rfile.read(length) if length else b""
            headers = {name.lower(): value for name, value in self.headers.items()}
            status, answer = listener.handle(method, self.path, headers, body)
            self._answer(status, answer)

        def _answer(self, status: HTTPStatus, answer: dict[str, object] | None) -> None:
            encoded = b"" if answer is None else json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            self._serve("GET")

        def do_POST(self) -> None:
            self._serve("POST")

    return Handler
