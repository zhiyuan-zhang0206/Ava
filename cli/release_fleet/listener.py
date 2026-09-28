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
within `MAX_BODY_BYTES` (otherwise `400` or `413`, nothing read).
"""

from __future__ import annotations

import json
import queue
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
from shared.log import logger

MAX_BODY_BYTES = 64 * 1024
_DECIMAL = re.compile(r"[0-9]+")
CAPABILITY_REFUSAL = (
    "the per-operation capability exchange over the coordinator channel is slice dbgen-8"
)
# Proof headers; their names are part of the channel protocol `ava-coordinator/1`.
ENROLLMENT_HEADER = "X-Ava-Enrollment"
TIMESTAMP_HEADER = "X-Ava-Timestamp"
NONCE_HEADER = "X-Ava-Nonce"
SIGNATURE_HEADER = "X-Ava-Signature"


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
        raise _Refused(HTTPStatus.UNAUTHORIZED, "the request carries no channel proof") from exc


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
        handler = _handler(self)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
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
            raise _Refused(HTTPStatus.NOT_FOUND, "no such coordinator route")
        if parts[3] != str(self.operation):
            raise _Refused(HTTPStatus.NOT_FOUND, "the listener serves another operation")
        unit = self._units.get(parts[5])
        if unit is None:
            raise _Refused(HTTPStatus.NOT_FOUND, "the unit takes no part in this operation")
        proof = _proof(headers)
        enrollment = self._enrollment(self.home, UnitIdentity(machine=unit.machine, home=unit.home))
        if enrollment is None:
            raise _Refused(HTTPStatus.UNAUTHORIZED, "the unit holds no enrollment on this gateway")
        try:
            verify_request(
                enrollment, proof, window=self._window, method=method, path=path, body=body
            )
        except ChannelRefusedError as exc:
            raise _Refused(HTTPStatus.UNAUTHORIZED, str(exc)) from exc
        return unit

    def handle(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[HTTPStatus, dict[str, object] | None]:
        """One request's status and JSON body; refusals never change state.

        `headers` are keyed by lowercased name.
        """
        try:
            return self._route(method, path, headers, body)
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


def _handler(listener: CoordinatorListener) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ava-coordinator/1"

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
