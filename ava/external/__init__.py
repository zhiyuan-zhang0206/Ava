"""Attach local Python tools to a trusted external controller lease."""

# Package door. This `__init__` is the `ava.external` attachment API; `state` is
# its checkpoint-compatible plugin-delta codec and snapshot reader, shared with
# the agent kernel's takeover return path (`agent.impersonation`).

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from threading import Lock, local
from types import TracebackType
from typing import Any, Self
from uuid import uuid4

import ava
from ava.sdk_surface import process_context
from ava.sdk_surface import settings as sdk_settings
from ava.sdk_surface.settings import database
from base.agents import impersonation as control
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.cluster.machine import machine_name
from base.config.agent_pins import resolve_agent_config_pins
from base.log import logger
from base.native_process.ownership import process_metadata
from base.packages.plugins.config_view import PluginConfigView, resolve_agent_plugin_pins

from .state import (
    apply_plugin_delta,
    decode_plugin_delta,
    encode_plugin_delta,
    load_snapshot,
)

__all_for_ava__ = ["attach", "Attachment"]

_attachment_lock = Lock()
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
    if "base.telemetry" not in sys.modules:
        return
    try:
        from base import telemetry

        telemetry.sync(bounded=True)
        if "base.telemetry.otlp.telemetry_otlp" in sys.modules:
            from base.telemetry.otlp import telemetry_otlp

            telemetry_otlp.finalize()
    except Exception:
        logger.opt(exception=True).warning(
            "external attachment: telemetry delivery before detach failed; "
            "queued records stay in the JSONL mirror"
        )


def _refuse_unless_attachable() -> AvaContext | None:
    """The process's own context, when nothing forbids attaching to it: no attachment yet, and
    no native agent runtime."""
    if ava.is_host_process():
        raise RuntimeError("a native agent runtime cannot attach an external controller")
    bound = getattr(ava, "context", None)
    identity = None if bound is None else bound.identity
    if identity is not None and identity.lease is not None:
        raise RuntimeError("this process already has an external attachment")
    if identity is not None and identity.agent_id is not None and identity.owns_loop:
        raise RuntimeError("a native agent runtime cannot attach an external controller")
    return bound


class Attachment:
    """One local SDK attachment; use a context manager or explicitly close it.

    `flush()` journals plugin state updates under the lease's version check.
    The native graph applies that journal after release or expiry. Ordinary
    SDK effects happen when called; they are not rolled back by an exception.
    """

    def __init__(self, lease_id: str) -> None:
        import ava

        bound = _refuse_unless_attachable()
        self.lease_id = lease_id
        self._closed = False
        self._closing = False
        self._stack = ExitStack()
        # The attached agent's pins and plugin-config view (`ava.sdk_surface.settings._attached` reads it through the lease), once loaded.
        self.config: tuple[Mapping[str, Any], PluginConfigView] | None = None
        self._event_participant: Any = None
        # The process's own context, put back at detach; `_bound` says this attachment bound one.
        self._prior_context = bound
        self._bound = False
        # The agent state class the snapshot loaded into (set before the constructor returns).
        self._state_cls: type[Any]
        if ava.in_exec_turn():
            raise RuntimeError("an exec turn cannot attach an external controller")
        if not _attachment_lock.acquire(blocking=False):
            raise RuntimeError("this process already has an external attachment")
        try:
            lease = self._lease()
            self.agent_id = int(lease["agent_id"])
            self.session_id = int(lease["session_id"])
            self._version = int(lease["delta_version"])
            self._bind_borrowed_context()
            # Native load: load_snapshot below rebuilds the checkpoint state
            # (build_agent_state().model_validate), which needs the plugins'
            # state fields registered — the surface-only default would silently
            # drop them (review finding, #2616).
            ava.ensure_plugins_loaded(surface=False)
            state, overlay, birth = load_snapshot(self.agent_id)
            self._state_cls = type(state)
            self._resolve_config(overlay, birth)
            # Native applies journal entries only after the controller releases
            # the lease. Already applied entries belong to the checkpoint.
            receipt = state.impersonation_applied
            checkpoint_version = receipt["version"] if receipt.get("lease_id") == lease_id else 0
            applied = max(lease["applied_version"], checkpoint_version)
            for encoded in lease["plugin_delta"][applied:]:
                apply_plugin_delta(state, decode_plugin_delta(encoded, self._state_cls))
            self._validate()
            self._open_event_participant()
            ava.state, ava.state_update = state, {}
        except BaseException:
            self._detach()
            raise

    def _resolve_config(
        self, overlay: Mapping[str, Any] | None, birth: Mapping[str, Any] | None
    ) -> None:
        from ava.sdk_surface.install import installed

        installation = installed()
        if installation is None:
            raise RuntimeError("the SDK surface was not installed for the attachment")
        self.config = (
            resolve_agent_config_pins(overlay, birth),
            PluginConfigView(
                installation.configs, resolve_agent_plugin_pins(overlay, installation.configs)
            ),
        )

    def _bind_borrowed_context(self) -> None:
        """Bind the borrowed identity for the attachment's lifetime (`_detach` puts the process's
        own context back), over whatever identity the process already had."""
        borrowed = ExternalLease(
            agent_id=self.agent_id, validate=self._validate, config=lambda: self.config
        )
        bound = self._prior_context
        own = (None if bound is None else bound.identity) or AgentIdentity(
            agent_id=None, owns_loop=True
        )
        ava.bind_context(
            dataclasses.replace(
                bound or AvaContext(clients=process_context.process_clients()),
                catalog=sdk_settings.model_catalog(),
                identity=dataclasses.replace(own, lease=borrowed),
            )
        )
        self._bound = True

    def _lease(self) -> dict[str, Any]:
        lease = control.require_active(database(), self.lease_id, process_metadata())
        if lease["machine"] != machine_name():
            raise RuntimeError(f"external SDK must run on agent machine {lease['machine']!r}")
        return lease

    def _validate(self, *, allow_closing: bool = False) -> int:
        if self._closed:
            raise RuntimeError("external attachment is closed")
        if self._closing and not allow_closing:
            from base.agents.impersonation.manifest import local_sdk_call_was_admitted

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
            encoded = encode_plugin_delta(ava.state_update, self._state_cls)
            control.merge_plugin_delta(
                database(),
                self.lease_id,
                process_metadata(),
                encoded,
                expected_version=self._version,
            )
            self._version += 1
            ava.state_update.clear()

    def close(self) -> None:
        """Flush plugin changes and remove the borrowed identity even if flushing fails."""
        if self._closed or self._closing:
            return
        try:
            self._begin_event_participant_close()
            was_permitted = _close_flush_permitted()
            _close_flush_permission.allowed = True
            try:
                self.flush()
            finally:
                _close_flush_permission.allowed = was_permitted
        finally:
            try:
                # Receipt closure precedes the best-effort observation flush: the
                # receipt seals the event log, which no longer depends on delivery.
                self._seal_event_participant()
            finally:
                try:
                    _deliver_telemetry_before_detach()
                finally:
                    self._detach()

    def _open_event_participant(self) -> None:
        """Register this controller before it can emit a protocol-v1 event."""
        from base.agents.impersonation.manifest import (
            LocalParticipant,
            bind_local_participant,
            is_log_native,
            open_local_participant,
        )

        if not is_log_native(self._lease()):
            return
        source_key = f"attachment:{process_metadata()['pid']}:{uuid4().hex}"
        db = database()
        if open_local_participant(db, self.lease_id, agent_id=self.agent_id, source_key=source_key):
            participant = LocalParticipant(
                lease_id=self.lease_id,
                agent_id=self.agent_id,
                session_id=self.session_id,
                source_key=source_key,
                db=db,
            )
            bind_local_participant(participant)
            self._event_participant = participant

    def _seal_event_participant(self) -> None:
        """Close admission, drain local SDK work, then seal the durable receipt."""
        if self._event_participant is None:
            return
        from base.agents.impersonation.manifest import (
            close_local_participant_admission,
            seal_local_participant,
        )
        from base.config import settings

        drained = close_local_participant_admission(
            self._event_participant,
            timeout=settings.general.impersonation_event_seal_wait_seconds,
        )
        # A call still running after the wait seals its own source when it drains; the
        # wait never turns a live source into an empty or failed receipt. An ended lease
        # that keeps an open source is signalled by state (impersonation_event_log_incomplete).
        if drained:
            seal_local_participant(self._event_participant)

    def _begin_event_participant_close(self) -> None:
        """Atomically start close and fence new SDK admission."""
        if self._event_participant is None:
            self._closing = True
            return
        from base.agents.impersonation.manifest import begin_local_participant_close

        # The gate lock makes setting `_closing` and closing admission one
        # linearization point. A call admitted before it may drain; one that
        # starts after it has no admission and `_validate` rejects it.
        begin_local_participant_close(
            self._event_participant,
            lambda: setattr(self, "_closing", True),
        )

    def _detach(self) -> None:
        """Drop local bindings without reading or writing the lease."""
        import ava

        if self._closed:
            return
        self._closed = True
        try:
            if self._event_participant is not None:
                from base.agents.impersonation.manifest import unbind_local_participant

                unbind_local_participant(self._event_participant)
                self._event_participant = None
            if self._bound and self._prior_context is not None:
                ava.bind_context(self._prior_context)
            elif self._bound:
                # The process had no context of its own: the one this attachment made, and the
                # clients it built, end with the attachment.
                context = ava.unbind_context()
                if context is not None:
                    context.clients.close()
            ava.unbind_exec_turn()
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
    from base.agents.impersonation.sessions import private_id

    if isinstance(session_id, int) and not isinstance(session_id, bool) and agent_id is not None:
        return Attachment(private_id(database(), agent_id, session_id))
    if isinstance(session_id, str) and agent_id is None:
        return Attachment(session_id)
    raise ValueError("attach requires an agent_id and an integer session_id")
