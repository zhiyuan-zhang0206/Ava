"""A unit executor's side of the coordinator channel.

Every request carries a channel proof signed with the unit's own installed
enrollment (`shared.cluster.authority.unit.load_unit_enrollment`), never the
human bearer or a write generation. Failures are typed so the follower can
tell a coordinator that is simply away (keep polling within the executor's
lifetime) from one that refuses this unit (hold) or a report that answers an
instruction the coordinator has since replaced (pull again).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import NoReturn
from uuid import UUID

from cli.release_fleet.listener import CAPABILITY_REFUSAL, proof_headers, route
from cli.release_fleet.policy import UnitKey
from cli.release_fleet.progress import Instruction, Report
from cli.release_fleet.request import CoordinatorEndpoint
from shared.cluster.authority.channel import ChannelRefusedError, sign_request
from shared.cluster.authority.unit import Enrollment

_TIMEOUT_S = 10.0
_MAX_ANSWER_BYTES = 64 * 1024


class CoordinatorAwayError(RuntimeError):
    """The listener did not answer: the coordinator is between runs or unreachable."""


class StaleReportError(RuntimeError):
    """The coordinator no longer holds the instruction this report answers."""


class CapabilityDeferredError(RuntimeError):
    """The coordinator does not exchange capabilities yet (slice dbgen-8)."""


class CoordinatorClient:
    def __init__(
        self,
        endpoint: CoordinatorEndpoint,
        operation: UUID,
        unit: UnitKey,
        enrollment: Enrollment,
        *,
        timeout_s: float = _TIMEOUT_S,
    ) -> None:
        if (enrollment.unit.machine, enrollment.unit.home) != unit.order:
            raise ChannelRefusedError("the installed enrollment belongs to another unit")
        self.endpoint = endpoint
        self.operation = operation
        self.unit = unit
        self._enrollment = enrollment
        self._timeout_s = timeout_s

    def _call(self, method: str, suffix: str, body: bytes = b"") -> tuple[int, bytes]:
        path = route(self.operation, self.unit, suffix)
        proof = sign_request(
            self._enrollment, operation=str(self.operation), method=method, path=path, body=body
        )
        request = urllib.request.Request(  # noqa: S310 — the captured coordinator endpoint, http only
            self.endpoint.url + path,
            data=body if method == "POST" else None,
            headers={"Content-Type": "application/json", **proof_headers(proof)},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as answer:  # noqa: S310 — same URL
                return answer.status, answer.read(_MAX_ANSWER_BYTES)
        except urllib.error.HTTPError as refused:
            return refused.code, refused.read(_MAX_ANSWER_BYTES)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise CoordinatorAwayError(f"the coordinator listener did not answer: {exc}") from exc

    @staticmethod
    def _refusal(status: int, body: bytes) -> str:
        try:
            return str(json.loads(body)["error"])
        except (ValueError, KeyError, TypeError):
            return f"HTTP {status}"

    def _refuse(self, status: int, body: bytes) -> NoReturn:
        if status >= 500 and status != 501:
            raise CoordinatorAwayError(f"the coordinator listener failed: HTTP {status}")
        raise ChannelRefusedError(
            f"the coordinator refused this unit: {self._refusal(status, body)}"
        )

    def instruction(self) -> Instruction | None:
        """The unit's current instruction, or None before the coordinator issued one."""
        status, body = self._call("GET", "")
        if status == 204:
            return None
        if status != 200:
            self._refuse(status, body)
        instruction = Instruction.model_validate_json(body)
        if instruction.operation != self.operation or instruction.unit != self.unit:
            raise ChannelRefusedError(
                "the coordinator served another operation's or unit's instruction"
            )
        return instruction

    def report(self, report: Report) -> None:
        status, body = self._call("POST", "/report", report.model_dump_json().encode())
        if status == 409:
            raise StaleReportError(self._refusal(status, body))
        if status != 202:
            self._refuse(status, body)

    def capability(self) -> bytes:
        """The sealed answer to this unit's capability request (its shape is slice dbgen-8's)."""
        status, body = self._call("POST", "/capability")
        if status == 501:
            raise CapabilityDeferredError(CAPABILITY_REFUSAL)
        if status != 200:
            self._refuse(status, body)
        return body
