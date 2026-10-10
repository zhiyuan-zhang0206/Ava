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
from typing import Any, Self, cast
from uuid import uuid4

import ava
from ava.sdk_surface import process_context
from ava.sdk_surface import settings as sdk_settings
from base.agents import impersonation as control
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet, DatabaseFactory
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.clock import Clock, clock_config_from_boot
from base.cluster.machine import machine_name
from base.config import ConfigBoot
from base.config.agent_pins import resolve_agent_config_pins
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.log import logger
from base.native_process.code_version import CodeVersion
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


def _deliver_telemetry_before_detach(*, clients: ClientSet) -> None:
    """Ship an external attachment's tail records while its interpreter lives.

    An SDK call can be the final operation in `ava impersonate exec`. The normal
    atexit drain has already proved insufficient for first-time OTLP setup in
    that shape, while an attachment close still runs in the live interpreter.
    Mirror the exec-child delivery order without importing telemetry for an
    attachment that emitted no records.
    """
    if "base.telemetry" not in sys.modules:
        return
    from base import telemetry

    owned = clients.sync_events()
    if owned.status is telemetry.DrainStatus.UNFINISHED:
        logger.warning(
            "external attachment: owned telemetry delivery is unfinished; "
            "queued records may be lost or land later"
        )
    try:
        result = telemetry.sync(bounded=True)
        if result.status is telemetry.DrainStatus.UNFINISHED:
            logger.warning(
                "external attachment: ordinary telemetry delivery is unfinished; "
                "queued records may be lost or land later"
            )
        if "base.telemetry.otlp.telemetry_otlp" in sys.modules:
            from base.telemetry.otlp import telemetry_otlp

            telemetry_otlp.finalize()
    except Exception:
        logger.opt(exception=True).warning(
            "external attachment: telemetry delivery before detach failed; "
            "ordinary queued records may be lost or land later"
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


def _attachment_database(bound: AvaContext | None, *, config: ConfigBoot | None) -> DatabaseFactory:
    """Borrow an existing owner's factory or create this attachment's fresh gate.

    Independently opened attachments do not share a check timestamp. A factory
    can refresh its configuration and return many handles without resetting the
    same attachment's admission gate.
    """
    if bound is not None:
        return bound.clients.database
    if config is None:
        raise RuntimeError("an independent attachment needs its configuration owner")
    version = CodeVersion(ava.loaded_code_image())
    gate = ProcessDbGate(version=version.get, process="unknown")
    return lambda: Database(db_config_from_boot(config), gate=gate)


class Attachment:
    """One local SDK attachment; use a context manager or explicitly close it.

    `flush()` journals plugin state updates under the lease's version check.
    The native graph applies that journal after release or expiry. Ordinary
    SDK effects happen when called; they are not rolled back by an exception.
    """

    def __init__(
        self, lease_id: str, *, agent_id: int | None = None, session_id: int | None = None
    ) -> None:
        import ava

        bound = _refuse_unless_attachable()
        if session_id is not None and agent_id is None:
            raise ValueError("an integer session requires an agent_id")
        self.lease_id = lease_id
        self._closed = False
        self._closing = False
        self._stack = ExitStack()
        # The attached agent's pins and plugin-config view (`ava.sdk_surface.settings._attached` reads it through the lease), once loaded.
        self.config: tuple[Mapping[str, Any], PluginConfigView] | None = None
        self._event_gate: Any = None
        self._call_lock = Lock()
        # The process's own context, put back at detach; `_bound` says this attachment bound one.
        self._prior_context = bound
        self._bound = False
        self._own_clients: ClientSet | None = None
        # The agent state class the snapshot loaded into (set before the constructor returns).
        self._state_cls: type[Any]
        if ava.in_exec_turn():
            raise RuntimeError("an exec turn cannot attach an external controller")
        if not _attachment_lock.acquire(blocking=False):
            raise RuntimeError("this process already has an external attachment")
        try:
            self._initialize(bound, agent_id=agent_id, session_id=session_id)
        except BaseException as primary:
            try:
                self._detach()
            except BaseException as secondary:
                primary.add_note(f"Attachment constructor cleanup also failed: {secondary!r}")
            raise

    def _initialize(
        self, bound: AvaContext | None, *, agent_id: int | None, session_id: int | None
    ) -> None:
        config = None if bound is not None else ConfigBoot()
        if config is not None:
            config.boot()
        self._clock_factory = (
            bound.require_clock
            if bound is not None
            else (lambda: Clock(clock_config_from_boot(cast(ConfigBoot, config))))
        )
        self._database = _attachment_database(bound, config=config)
        if session_id is not None:
            from base.agents.impersonation.sessions import private_id

            self.lease_id = private_id(self._database(), cast(int, agent_id), session_id)
        if bound is None:
            self._own_clients = process_context.process_clients(
                database=self._database, config=config
            )
        lease = self._lease()
        self.agent_id = int(lease["agent_id"])
        self.session_id = int(lease["session_id"])
        self._version = int(lease["delta_version"])
        # Native load: load_snapshot below rebuilds the checkpoint state
        # (build_agent_state().model_validate), which needs the plugins'
        # state fields registered — the surface-only default would silently
        # drop them (review finding, #2616).
        self._bind_borrowed_context()
        ava.ensure_plugins_loaded(surface=False, config=config, clock_factory=self._clock_factory)
        self._bind_catalog()
        if config is None:
            from ava.sdk_surface.settings import config_authority

            authority = config_authority()
            self._event_seal_wait_reader = lambda: cast(
                float, authority.service_field_value("impersonation_event_seal_wait_seconds")
            )
        else:
            self._event_seal_wait_reader = lambda: (
                config.view.general.impersonation_event_seal_wait_seconds
            )
        state, overlay, birth = load_snapshot(self.agent_id, database=self._database())
        self._state_cls = type(state)
        self._resolve_config(overlay, birth)
        # Native applies journal entries only after the controller releases
        # the lease. Already applied entries belong to the checkpoint.
        receipt = state.impersonation_applied
        checkpoint_version = receipt["version"] if receipt.get("lease_id") == self.lease_id else 0
        applied = max(lease["applied_version"], checkpoint_version)
        for encoded in lease["plugin_delta"][applied:]:
            apply_plugin_delta(state, decode_plugin_delta(encoded, self._state_cls))
        self._validate()
        self._open_event_participant()
        ava.state, ava.state_update = state, {}

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
                bound or self._new_context(),
                identity=dataclasses.replace(own, lease=borrowed),
                sdk_capture=self,
            )
        )
        self._bound = True

    def _new_context(self) -> AvaContext:
        if self._own_clients is None:
            raise RuntimeError("an unbound attachment has no owned clients")
        return AvaContext(clients=self._own_clients, clock_factory=self._clock_factory)

    def _bind_catalog(self) -> None:
        """Attach the installed catalog after identity and native plugin registration."""
        context = ava.context
        ava.bind_context(dataclasses.replace(context, catalog=sdk_settings.model_catalog()))

    def admit_sdk_call(self) -> Any:
        """Snapshot this call's original gate before its body can run."""
        with self._call_lock:
            if self._closed or self._closing:
                raise RuntimeError("external attachment is closing or closed")
            gate = self._event_gate
            if gate is None:
                self._validate()
                return None
            with gate.condition:
                if self._closed or self._closing or gate.admission_closed:
                    raise RuntimeError("external attachment is closing or closed")
                self._validate()
                return gate.admit()

    def capture_local_event(self, event: Any) -> Any:
        """Capture a direct audit using this attachment's own retained receipt."""
        from base.agents.impersonation.manifest import capture_local_event

        return capture_local_event(event, gate=self._event_gate)

    def _lease(self) -> dict[str, Any]:
        lease = control.require_active(self._database(), self.lease_id, process_metadata())
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
        self._flush(allow_closing=_close_flush_permitted())

    def _flush(self, *, allow_closing: bool) -> None:
        """Stage plugin state; attachment close alone may finish this operation."""
        import ava

        if self._closing and not allow_closing:
            raise RuntimeError("external attachment is closing")
        self._validate()
        if not isinstance(ava.state_update, dict):
            raise TypeError("external plugin state update must be a dict")
        if ava.state_update:
            encoded = encode_plugin_delta(ava.state_update, self._state_cls)
            control.merge_plugin_delta(
                self._database(),
                self.lease_id,
                process_metadata(),
                encoded,
                expected_version=self._version,
            )
            self._version += 1
            ava.state_update.clear()

    def close(self) -> None:
        """Flush plugin changes and remove the borrowed identity even if flushing fails."""
        if self._closed:
            self._close_owned_clients()
            return
        if self._closing:
            return
        primary: BaseException | None = None
        try:
            self._begin_event_participant_close()
            was_permitted = _close_flush_permitted()
            _close_flush_permission.allowed = True
            try:
                self.flush()
            finally:
                _close_flush_permission.allowed = was_permitted
        except BaseException as exc:
            primary = exc
        # Receipt closure precedes observation delivery and detach. Every cleanup
        # runs; a later failure cannot replace the exact original close error.
        for cleanup in (
            self._seal_event_participant,
            self._deliver_telemetry,
            self._detach,
        ):
            try:
                cleanup()
            except BaseException as secondary:
                if primary is None:
                    primary = secondary
                else:
                    primary.add_note(f"Attachment close cleanup also failed: {secondary!r}")
        if primary is not None:
            raise primary

    def _deliver_telemetry(self) -> None:
        clients = self._own_clients
        if self._prior_context is not None:
            clients = self._prior_context.clients
        if clients is None:
            raise RuntimeError("the attachment has no event producer owner")
        _deliver_telemetry_before_detach(clients=clients)

    def _open_event_participant(self) -> None:
        """Register this controller before it can emit a protocol-v1 event."""
        from base.agents.impersonation.manifest import (
            LocalCaptureGate,
            LocalParticipant,
            is_log_native,
            open_local_participant,
        )

        if not is_log_native(self._lease()):
            return
        source_key = f"attachment:{process_metadata()['pid']}:{uuid4().hex}"
        db = self._database()
        if open_local_participant(db, self.lease_id, agent_id=self.agent_id, source_key=source_key):
            participant = LocalParticipant(
                lease_id=self.lease_id,
                agent_id=self.agent_id,
                session_id=self.session_id,
                source_key=source_key,
                db=db,
            )
            self._event_gate = LocalCaptureGate(participant)

    def _seal_event_participant(self) -> None:
        """Close admission, drain local SDK work, then seal the durable receipt."""
        if self._event_gate is None:
            return
        from base.agents.impersonation.manifest import (
            close_local_participant_admission,
            seal_local_participant,
        )

        drained = close_local_participant_admission(
            self._event_gate,
            timeout=self._event_seal_wait_reader(),
        )
        # A call still running after the wait seals its own source when it drains; the
        # wait never turns a live source into an empty or failed receipt. An ended lease
        # that keeps an open source is signalled by state (impersonation_event_log_incomplete).
        if drained:
            seal_local_participant(self._event_gate)

    def _begin_event_participant_close(self) -> None:
        """Atomically start close and fence new SDK admission."""
        if self._event_gate is None:
            with self._call_lock:
                self._closing = True
            return
        from base.agents.impersonation.manifest import begin_local_participant_close

        # The gate lock makes setting `_closing` and closing admission one
        # linearization point. A call admitted before it may drain; one that
        # starts after it is rejected by its public call admission owner.
        begin_local_participant_close(
            self._event_gate,
            lambda: setattr(self, "_closing", True),
        )

    def _detach(self) -> None:
        """Drop local bindings without reading or writing the lease."""
        import ava

        if self._closed:
            return
        self._closed = True
        primary: BaseException | None = None
        try:
            for cleanup in (
                self._restore_prior_context,
                ava.unbind_exec_turn,
                self._stack.close,
                self._close_owned_clients,
            ):
                try:
                    cleanup()
                except BaseException as secondary:
                    if primary is None:
                        primary = secondary
                    else:
                        primary.add_note(f"Attachment detach cleanup also failed: {secondary!r}")
        finally:
            _attachment_lock.release()
        if primary is not None:
            raise primary

    def _restore_prior_context(self) -> None:
        if self._bound and self._prior_context is not None:
            ava.bind_context(self._prior_context)
        elif self._bound:
            ava.unbind_context()

    def _close_owned_clients(self) -> None:
        if self._own_clients is not None:
            self._own_clients.close()

    def __enter__(self) -> Self:
        try:
            self._validate()
        except BaseException as primary:
            try:
                self._detach()
            except BaseException as secondary:
                primary.add_note(f"Attachment entry cleanup also failed: {secondary!r}")
            raise
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _trace: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except BaseException as secondary:
            if _error is None:
                raise
            _error.add_note(f"Attachment context cleanup also failed: {secondary!r}")


def attach(session_id: int | str, *, agent_id: int | None = None) -> Attachment:
    """Borrow an active, unexpired agent identity in this local Python process.

    Use the session's integer id together with its owning agent id. Attaching
    needs no credential: the caller must descend from the session's recorded
    controller process tree, rechecked on every lease use. The attachment loads
    the agent's saved configuration and plugin state. SDK calls and
    plugin-state operations recheck the lease. Direct reads of loaded Python
    objects do not. Attaching never renews the lease.
    """
    if isinstance(session_id, int) and not isinstance(session_id, bool) and agent_id is not None:
        return Attachment("", agent_id=agent_id, session_id=session_id)
    if isinstance(session_id, str) and agent_id is None:
        return Attachment(session_id)
    raise ValueError("attach requires an agent_id and an integer session_id")
