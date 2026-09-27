"""Attach local Python tools to a trusted external controller lease."""

from __future__ import annotations

import sys
from contextlib import ExitStack, suppress
from threading import Lock, Timer, local
from types import TracebackType
from typing import Any, Self
from uuid import uuid4

from shared.agents import impersonation as control
from shared.config.turn_view import bind_agent_config, resolve_agent_config_pins
from shared.machine import machine_name
from shared.plugin_config_view import bind_agent_plugin_config, resolve_agent_plugin_pins
from shared.proc_tree import process_metadata

from . import agent_identity
from .external_state import (
    apply_plugin_delta,
    decode_plugin_delta,
    encode_plugin_delta,
    load_snapshot,
)

__all_for_ava__ = ["attach", "Attachment"]

_attachment_lock = Lock()
_active_attachment: Attachment | None = None
_close_flush_permission = local()


def _close_flush_permitted() -> bool:
    """Whether this synchronous close operation may finish its own flush."""
    return bool(getattr(_close_flush_permission, "allowed", False))


def _deliver_telemetry_before_detach() -> None:
    """Ship an external attachment's tail records while its interpreter lives.

    An SDK call can be the final operation in `ava impersonate exec`. The normal
    atexit drain has already proved insufficient for first-time OTLP setup in
    that shape, while an attachment close still runs in the live interpreter.
    Mirror the exec-child delivery order without importing telemetry for an
    attachment that emitted no records.
    """
    if "shared.telemetry" not in sys.modules:
        return
    with suppress(Exception):
        from shared import telemetry

        telemetry.sync(bounded=True)
        if "shared.telemetry.otlp.telemetry_otlp" in sys.modules:
            from shared.telemetry.otlp import telemetry_otlp

            telemetry_otlp.finalize()


class Attachment:
    """One local SDK attachment; use a context manager or explicitly close it.

    `flush()` journals plugin state updates under the lease's version check.
    The native graph applies that journal after release or expiry. Ordinary
    SDK effects happen when called; they are not rolled back by an exception.
    """

    def __init__(self, lease_id: str) -> None:
        import ava

        global _active_attachment  # noqa: PLW0603 — one process attachment, guarded by _attachment_lock
        if agent_identity._external_identity is not None:
            raise RuntimeError("this process already has an external attachment")
        if agent_identity.current_turn_agent_id() is not None or (
            agent_identity._agent_id is not None and agent_identity._owns_loop
        ):
            raise RuntimeError("a native agent runtime cannot attach an external controller")
        self.lease_id = lease_id
        self._closed = False
        self._closing = False
        self._stack = ExitStack()
        self._manifest_participant: Any = None
        if not _attachment_lock.acquire(blocking=False):
            raise RuntimeError("this process already has an external attachment")
        self._prior_state = ava.state
        self._prior_update = ava.state_update
        try:
            lease = self._lease()
            self.agent_id = int(lease["agent_id"])
            self.session_id = int(lease["session_id"])
            _active_attachment = self
            self._version = int(lease["delta_version"])
            agent_identity._external_identity = self._validate
            agent_identity._external_agent_id = self.agent_id
            # Native load: load_snapshot below rebuilds the checkpoint state
            # (build_agent_state().model_validate), which needs the plugins'
            # state fields registered — the surface-only default would silently
            # drop them (review finding, #2616).
            ava.ensure_plugins_loaded(surface=False)
            state, overlay, birth = load_snapshot(self.agent_id)
            self._stack.enter_context(bind_agent_config(resolve_agent_config_pins(overlay, birth)))
            self._stack.enter_context(bind_agent_plugin_config(resolve_agent_plugin_pins(overlay)))
            # Native applies journal entries only after the controller releases
            # the lease. Already applied entries belong to the checkpoint.
            receipt = state.impersonation_applied
            checkpoint_version = receipt["version"] if receipt.get("lease_id") == lease_id else 0
            applied = max(lease["applied_version"], checkpoint_version)
            for encoded in lease["plugin_delta"][applied:]:
                apply_plugin_delta(state, decode_plugin_delta(encoded))
            self._validate()
            self._open_manifest_participant()
            ava.state, ava.state_update = state, {}
        except BaseException:
            self._detach()
            raise

    def _lease(self) -> dict[str, Any]:
        lease = control.require_active(self.lease_id, process_metadata())
        if lease["machine"] != machine_name():
            raise RuntimeError(f"external SDK must run on agent machine {lease['machine']!r}")
        return lease

    def _validate(self, *, allow_closing: bool = False) -> int:
        if self._closed:
            raise RuntimeError("external attachment is closed")
        if self._closing and not allow_closing:
            from shared.agents.impersonation_manifest import local_sdk_call_was_admitted

            if not local_sdk_call_was_admitted():
                raise RuntimeError("external attachment is closing")
        lease = self._lease()
        if lease["delta_version"] != self._version:
            raise RuntimeError(
                "another attachment changed plugin state; attach again before acting"
            )
        return self.agent_id

    def flush(self) -> None:
        """Durably stage this attachment's new plugin delta; never renew the lease."""
        self._flush(allow_closing=_close_flush_permitted())

    def _flush(self, *, allow_closing: bool) -> None:
        """Stage plugin state; attachment close alone may finish this operation."""
        import ava

        self._validate(allow_closing=allow_closing)
        if not isinstance(ava.state_update, dict):
            raise TypeError("external plugin state update must be a dict")
        if ava.state_update:
            encoded = encode_plugin_delta(ava.state_update)
            control.merge_plugin_delta(
                self.lease_id, process_metadata(), encoded, expected_version=self._version
            )
            self._version += 1
            ava.state_update = {}

    def close(self) -> None:
        """Flush plugin changes and remove the borrowed identity even if flushing fails."""
        if self._closed or self._closing:
            return
        try:
            self._begin_manifest_participant_close()
            was_permitted = _close_flush_permitted()
            _close_flush_permission.allowed = True
            try:
                self.flush()
            finally:
                _close_flush_permission.allowed = was_permitted
        finally:
            try:
                # Receipt closure precedes the best-effort delivery flush: the
                # receipt is the emitted-event census, while sync only reduces
                # ordinary observation latency and cannot certify arrival.
                self._seal_manifest_participant()
            finally:
                try:
                    _deliver_telemetry_before_detach()
                finally:
                    self._detach()

    def _open_manifest_participant(self) -> None:
        """Register this controller before it can emit a protocol-v1 event."""
        from shared.agents.impersonation_manifest import (
            LocalParticipant,
            bind_local_participant,
            is_protocol_v1,
            open_local_participant,
        )

        if not is_protocol_v1(self._lease()):
            return
        source_key = f"attachment:{process_metadata()['pid']}:{uuid4().hex}"
        if open_local_participant(self.lease_id, agent_id=self.agent_id, source_key=source_key):
            participant = LocalParticipant(
                lease_id=self.lease_id,
                agent_id=self.agent_id,
                session_id=self.session_id,
                source_key=source_key,
            )
            bind_local_participant(participant)
            self._manifest_participant = participant

    def _seal_manifest_participant(self) -> None:
        """Close admission, drain local SDK work, then seal the durable receipt."""
        if self._manifest_participant is None:
            return
        from shared.agents.impersonation_manifest import (
            alert_if_participant_still_open,
            close_local_participant_admission,
            seal_local_participant,
        )
        from shared.config import settings

        # This timer is deliberately diagnostic-only. A live SDK finally may
        # outlast the detach wait and seal later; only a real capture failure
        # can mark its receipt failed.
        timer = Timer(
            settings.general.impersonation_event_manifest_seal_wait_seconds,
            alert_if_participant_still_open,
            args=(self._manifest_participant,),
        )
        timer.daemon = True
        timer.start()
        try:
            drained = close_local_participant_admission(
                self._manifest_participant,
                timeout=settings.general.impersonation_event_manifest_seal_wait_seconds,
            )
            if drained:
                seal_local_participant(self._manifest_participant)
            else:
                # The final admitted SDK finally seals when it drains. The
                # timer remains a diagnostic only; a live call never becomes
                # a synthetic failed or empty receipt at this deadline.
                alert_if_participant_still_open(self._manifest_participant)
        finally:
            timer.cancel()

    def _begin_manifest_participant_close(self) -> None:
        """Atomically start close and fence new SDK admission."""
        if self._manifest_participant is None:
            self._closing = True
            return
        from shared.agents.impersonation_manifest import begin_local_participant_close

        # The gate lock makes setting `_closing` and closing admission one
        # linearization point. A call admitted before it may drain; one that
        # starts after it has no admission and `_validate` rejects it.
        begin_local_participant_close(
            self._manifest_participant,
            lambda: setattr(self, "_closing", True),
        )

    def _detach(self) -> None:
        """Restore local bindings without reading or writing the lease."""
        import ava

        if self._closed:
            return
        global _active_attachment  # noqa: PLW0603 — one process attachment, guarded by _attachment_lock
        _active_attachment = None
        self._closed = True
        try:
            if self._manifest_participant is not None:
                from shared.agents.impersonation_manifest import unbind_local_participant

                unbind_local_participant(self._manifest_participant)
                self._manifest_participant = None
            agent_identity._external_identity = None
            agent_identity._external_agent_id = None
            ava.state, ava.state_update = self._prior_state, self._prior_update
            self._stack.close()
        finally:
            _attachment_lock.release()

    def __enter__(self) -> Self:
        try:
            self._validate()
        except BaseException:
            self._detach()
            raise
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _trace: TracebackType | None,
    ) -> None:
        self.close()


def attach(session_id: int | str, *, agent_id: int | None = None) -> Attachment:
    """Borrow an active, unexpired agent identity in this local Python process.

    Use the session's integer id together with its owning agent id. Attaching
    needs no credential: the caller must descend from the session's recorded
    controller process tree, rechecked on every lease use. The attachment loads
    the agent's saved configuration and plugin state. SDK calls and
    plugin-state operations recheck the lease. Direct reads of loaded Python
    objects do not. Attaching never renews the lease.
    """
    from shared.agents.impersonation.impersonation_sessions import private_id

    if isinstance(session_id, int) and not isinstance(session_id, bool) and agent_id is not None:
        return Attachment(private_id(agent_id, session_id))
    if isinstance(session_id, str) and agent_id is None:
        return Attachment(session_id)
    raise ValueError("attach requires an agent_id and an integer session_id")
