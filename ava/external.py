"""Attach local Python tools to a trusted external controller lease."""

from __future__ import annotations

import sys
from contextlib import ExitStack, suppress
from threading import Lock
from types import TracebackType
from typing import Any, Self

from shared import impersonation as control
from shared.config.turn_view import bind_agent_config, resolve_agent_config_pins
from shared.machine import machine_name
from shared.plugin_config_view import bind_agent_plugin_config, resolve_agent_plugin_pins
from shared.proc_tree import process_metadata

from . import _boot
from ._external_state import (
    apply_plugin_delta,
    decode_plugin_delta,
    encode_plugin_delta,
    load_snapshot,
)

__all_for_ava__ = ["attach", "Attachment"]

_attachment_lock = Lock()
_active_attachment: Attachment | None = None


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

        telemetry.sync()
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
        if _boot._external_identity is not None:
            raise RuntimeError("this process already has an external attachment")
        if _boot.current_turn_agent_id() is not None or (
            _boot._agent_id is not None and _boot._owns_loop
        ):
            raise RuntimeError("a native agent runtime cannot attach an external controller")
        self.lease_id = lease_id
        self._closed = False
        self._stack = ExitStack()
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
            _boot._external_identity = self._validate
            _boot._external_agent_id = self.agent_id
            # Native load: load_snapshot below rebuilds the checkpoint state
            # (build_agent_state().model_validate), which needs the plugins'
            # state fields registered — the surface-only default would silently
            # drop them (review finding, #2616).
            ava._ensure_plugins_loaded(surface=False)
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
            ava.state, ava.state_update = state, {}
        except BaseException:
            self._detach()
            raise

    def _lease(self) -> dict[str, Any]:
        lease = control.require_active(self.lease_id, process_metadata())
        if lease["machine"] != machine_name():
            raise RuntimeError(f"external SDK must run on agent machine {lease['machine']!r}")
        return lease

    def _validate(self) -> int:
        if self._closed:
            raise RuntimeError("external attachment is closed")
        lease = self._lease()
        if lease["delta_version"] != self._version:
            raise RuntimeError(
                "another attachment changed plugin state; attach again before acting"
            )
        return self.agent_id

    def flush(self) -> None:
        """Durably stage this attachment's new plugin delta; never renew the lease."""
        import ava

        self._validate()
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
        if self._closed:
            return
        try:
            self.flush()
        finally:
            try:
                _deliver_telemetry_before_detach()
            finally:
                self._detach()

    def _detach(self) -> None:
        """Restore local bindings without reading or writing the lease."""
        import ava

        if self._closed:
            return
        global _active_attachment  # noqa: PLW0603 — one process attachment, guarded by _attachment_lock
        _active_attachment = None
        self._closed = True
        try:
            _boot._external_identity = None
            _boot._external_agent_id = None
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
    from shared.impersonation_sessions import private_id

    if isinstance(session_id, int) and not isinstance(session_id, bool) and agent_id is not None:
        return Attachment(private_id(agent_id, session_id))
    if isinstance(session_id, str) and agent_id is None:
        return Attachment(session_id)
    raise ValueError("attach requires an agent_id and an integer session_id")
