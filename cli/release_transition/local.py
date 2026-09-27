"""One-host same-schema transition through the existing root lifecycle.

The initial adapter refuses remote enrollment. Persistent terminals, including
agents' coding sessions and schedule runners, are writers that do not survive a
release (decisions/2026-09-27-fleet-release-and-cutover-policies.md item 2):
the stop phase lets their work finish within a bound, stops root, then closes
every terminal. Their absence is closure evidence before selection, never
inferred from a successful root shutdown. Fleet fencing extends this boundary
rather than falling back to the mutable checkout updater.

The native root owner follows the operation's recorded executor kind: the
Linux boot unit (root_service.py) or the persistent macOS home helper
(root_macos.py). There is no host-probing fallback between them.
"""

from __future__ import annotations

from pathlib import Path

from cli.release_transition.journal import Journal, Operation
from cli.release_transition.native import helper_root
from cli.release_transition.request import Request, verify_pair
from shared.runtime_abi import current_abi
from shared.runtime_release import VerifiedRelease, activate_release, current_pointer

# Release writer closure bounds. Busy terminals get the completed-work wait
# while root still serves them, then the graceful cancel; whatever is live
# after the grace gets SIGKILL over its captured birth.
_TERMINAL_WORK_S = 30.0
_TERMINAL_GRACE_S = 10.0
_TERMINAL_KILL_S = 10.0


class LocalTransition:
    def __init__(self, request: Request) -> None:
        self.request = request
        self.home = Path(request.home)
        self.previous, self.candidate = verify_pair(request)

    def preflight(self) -> None:
        """Read-only operational gates; an unsupported request cannot stop work."""
        self.request.require_configuration()
        from cli.release_transition.identity import require_local_writers

        require_local_writers(self.request)

    def quiesce(self) -> None:
        from cli.release_transition.journal import read_operation
        from cli.release_transition.root_service import preflight
        from ops import agent_pause

        self.preflight()
        operation = read_operation(self.request.path)
        preflight(operation, self.previous, previous=True)
        preflight(operation, self.candidate, previous=False)
        # The selector can also be changed by callers outside journal admission.
        # Refuse before creating a maintenance hold or draining any workload.
        if current_pointer(self.home / "releases") != self.request.previous.selector:
            raise ValueError("prepared predecessor is not the selected release before quiescing")
        agent_pause.prepare(str(self.request.id), self.request.created_at)
        agent_pause.drain(str(self.request.id), self.request.created_at, 90, reap=True)

    def stop(self, operation: Operation) -> None:
        from cli.commands import maintenance as maintenance_commands
        from cli.commands import maintenance_stop
        from cli.commands.root_driver import require_root_absent
        from cli.release_transition import root_macos
        from ops import pty_close_notices
        from shared import maintenance, pause_owner
        from shared.maintenance_state import MaintenanceHold

        self.preflight()
        holder, at = str(self.request.id), self.request.created_at
        current = maintenance.require_operation(holder, at)
        hold = current.maintenance
        if hold is None:
            raise RuntimeError("release stop lost its maintenance cohort")
        if operation.direction == "previous" and hold.phase in {"starting", "ready"}:
            # Admission was never reopened. Preserve the same cohort and stop
            # the unsuccessful candidate before selecting its predecessor.
            pause_owner.change_maintenance(
                holder, at, hold, MaintenanceHold.decode(hold.encode() | {"phase": "stopping"})
            )
        # Observation only: in-flight terminal work may still need root.
        busy = maintenance_stop.await_terminal_work(_TERMINAL_WORK_S)
        darwin = helper_root(operation.launch)
        if darwin:
            # The stop request goes only to the authenticated recorded helper.
            root_macos.verified_helper(operation)
        # Root first: its reconcilers (schedules, pages) would re-arm a session.
        maintenance_commands.stop(holder, at, 90, gateway_last=True, keep_terminals=True)
        closed = maintenance_stop.close_release_terminals(
            holder,
            at,
            grace_s=_TERMINAL_GRACE_S,
            kill_s=_TERMINAL_KILL_S,
            reason=pty_close_notices.RELEASE_REASON,
        )
        print(
            f"Release closed persistent terminals: {sorted(closed.shells)}; "
            f"busy past the {_TERMINAL_WORK_S:.0f}s work bound: {busy}"
        )
        require_root_absent()
        if darwin:
            # Durable keeper stop intent: no restart, not even at login, until
            # the selected image's explicit seed.
            root_macos.require_stopped(operation)

    def preflight_authority(self) -> None:
        """Read-only: exactly one admitted write generation exists to fence."""
        from cli.release_transition import authority

        authority.preflight()

    def fence(self, journal: Journal) -> None:
        """Revoke the direction's write generation once its root is gone."""
        from cli.commands.root_driver import require_root_absent
        from cli.release_transition import authority

        self.request.require_configuration()
        require_root_absent()
        authority.fence(journal)

    def authorize(self, journal: Journal) -> None:
        """Admit a new write generation for the selected image before it starts."""
        from cli.commands.root_driver import require_root_absent
        from cli.release_transition import authority

        self.request.require_configuration()
        require_root_absent()
        operation = journal.operation
        target = (
            self.request.candidate if operation.direction == "candidate" else self.request.previous
        )
        authority.authorize(journal, target)

    def image(self, operation: Operation) -> VerifiedRelease:
        return self.candidate if operation.direction == "candidate" else self.previous

    def select(self, operation: Operation) -> None:
        from cli.commands.maintenance_stop import live_terminals
        from cli.commands.root_driver import require_root_absent

        self.preflight()
        require_root_absent()
        if terminals := live_terminals():
            raise RuntimeError(f"terminals appeared after release closure: {terminals}")
        target = (
            self.request.candidate if operation.direction == "candidate" else self.request.previous
        )
        predecessor = (
            self.request.previous if operation.direction == "candidate" else self.request.candidate
        )
        store = self.home / "releases"
        observed = current_pointer(store)
        if observed == target.selector:
            target.verify(self.home)
            return  # A crash after selector CAS is an observed completed effect.
        activate_release(
            store,
            target.artifact_digest,
            expected_current=predecessor.selector,
            manifest_digest=target.manifest_digest,
            host_abi=current_abi(),
            schema_digest=target.schema_digest,
        )

    def start(self, journal: Journal) -> None:
        """Journal access lets the macOS owner record helper custody around its effect."""
        self.request.require_configuration()
        from shared import maintenance

        operation = journal.operation
        current = maintenance.require_operation(str(self.request.id), self.request.created_at)
        if current.maintenance is None:
            raise RuntimeError("release start lost its maintenance cohort")
        if current.maintenance.phase == "stopped":
            maintenance.set_phase(str(self.request.id), self.request.created_at, "starting")
        elif current.maintenance.phase not in {"starting", "ready"}:
            raise RuntimeError("release start requires completed writer closure")
        if helper_root(operation.launch):
            from cli.release_transition import root_macos

            root_macos.start(journal, self.image(operation))
        else:
            from cli.release_transition.root_service import start

            start(operation, self.image(operation))

    def _observe_root(self, operation: Operation) -> None:
        if helper_root(operation.launch):
            from cli.release_transition.root_macos import observe
        else:
            from cli.release_transition.root_service import observe
        observe(operation, self.image(operation))

    def observe(self, operation: Operation) -> None:
        self.request.require_configuration()
        from shared import maintenance

        self._observe_root(operation)
        self.request.require_configuration()
        current = maintenance.require_operation(str(self.request.id), self.request.created_at)
        if current.maintenance is not None and current.maintenance.phase == "starting":
            maintenance.set_phase(str(self.request.id), self.request.created_at, "ready")
        if helper_root(operation.launch):
            from cli.release_transition.root_macos import restore_boot
        else:
            from cli.release_transition.root_service import restore_boot
        restore_boot(operation, self.image(operation))

    def resume(self, operation: Operation) -> None:
        self.request.require_configuration()
        from cli.commands import maintenance as maintenance_commands
        from shared import maintenance, pause_owner, start_serving

        # An executor may have died after recording this phase. A durable
        # serving marker or an earlier observation cannot admit work now.
        self._observe_root(operation)
        self.request.require_configuration()
        current = pause_owner.read()
        if (
            current.status == "resumed"
            and current.holder == str(self.request.id)
            and current.acquired_at == self.request.created_at
            and start_serving.is_serving()
        ):
            return
        maintenance.require_operation(str(self.request.id), self.request.created_at)
        maintenance_commands.resume(str(self.request.id), self.request.created_at, cancel=False)
