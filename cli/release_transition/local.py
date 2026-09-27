"""One home's release effects through the existing root lifecycle.

The fleet coordinator runs them for the gateway unit; a remote unit's executor
runs them for its own home. Persistent terminals, including agents' coding
sessions and schedule runners, are writers that do not survive a release
(decisions/2026-09-27-fleet-release-and-cutover-policies.md item 2): the stop
phase lets their work finish within the captured `close_s`, stops root, then
closes every terminal after `cancel_grace_s`. Their absence is closure
evidence before selection, never inferred from a successful root shutdown.

The maintenance hold is `(operation id, operation.maintenance_at)` on every
unit; a recovery after admission reopened drains again under a new timestamp.
The native root owner follows the operation's recorded executor kind: the
Linux boot unit (root_service.py) or the persistent macOS home helper
(root_macos.py). There is no host-probing fallback between them.
"""

from __future__ import annotations

from pathlib import Path

from cli.release_fleet.request import FleetRequest, UnitRequest
from cli.release_transition.authority_evidence import GenerationRef
from cli.release_transition.journal import Journal, Operation
from cli.release_transition.native import helper_root
from cli.release_transition.request import verify_pair
from shared.maintenance_state import MaintenanceHold
from shared.runtime_abi import current_abi
from shared.runtime_release import VerifiedRelease, activate_release, current_pointer

# Whatever is live after the cancel grace gets SIGKILL over its captured birth;
# this bounds only the kernel observation of that kill.
_TERMINAL_KILL_S = 10.0
# Bound of the ordinary root stop inside a release (services' own shutdown).
_ROOT_STOP_S = 90


class LocalTransition:
    def __init__(self, request: FleetRequest | UnitRequest) -> None:
        self.request = request
        self.home = Path(request.home)
        self.previous, self.candidate = verify_pair(request)

    def preflight(self) -> None:
        """Read-only operational gates; an unsupported request cannot stop work."""
        self.request.require_configuration()
        from cli.release_fleet.inventory import require_topology

        require_topology(self.request)

    def quiesce(self, operation: Operation) -> MaintenanceHold:
        """Drain local agents under the operation's hold; the drained hold is the cohort."""
        from cli.release_transition.root_service import preflight
        from ops import agent_pause
        from shared import maintenance

        self.preflight()
        preflight(operation, self.previous, previous=True)
        preflight(operation, self.candidate, previous=False)
        # The selector can also be changed by callers outside journal admission.
        # Refuse before creating a maintenance hold or draining any workload. A
        # recovery that drains again (from `watching`) finds the candidate selected.
        expected = (
            self.request.previous if operation.direction == "candidate" else self.request.candidate
        )
        if current_pointer(self.home / "releases") != expected.selector:
            raise ValueError("the recorded release is not the selected release before quiescing")
        holder, at = str(self.request.id), operation.maintenance_at
        agent_pause.prepare(holder, at)
        agent_pause.drain(holder, at, self.request.policy.drain_s, reap=True)
        current = maintenance.require_operation(holder, at).maintenance
        if current is None:
            raise RuntimeError("release drain lost its maintenance cohort")
        return current

    def stop(self, operation: Operation) -> None:
        from cli.commands import maintenance as maintenance_commands
        from cli.commands import maintenance_stop
        from cli.commands.root_driver import require_root_absent
        from cli.release_transition import root_macos
        from shared import maintenance, pause_owner

        self.preflight()
        policy = self.request.policy
        holder, at = str(self.request.id), operation.maintenance_at
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
        busy = maintenance_stop.await_terminal_work(float(policy.close_s))
        darwin = helper_root(operation.launch)
        if darwin:
            # The stop request goes only to the authenticated recorded helper.
            root_macos.verified_helper(operation)
        # Root first: its reconcilers (schedules, pages) would re-arm a session.
        maintenance_commands.stop(holder, at, _ROOT_STOP_S, gateway_last=True, keep_terminals=True)
        closed = maintenance_stop.close_release_terminals(
            holder, at, grace_s=float(policy.cancel_grace_s), kill_s=_TERMINAL_KILL_S
        )
        print(
            f"Release closed persistent terminals: {sorted(closed.shells)}; "
            f"busy past the {policy.close_s}s work bound: {busy}"
        )
        require_root_absent()
        if darwin:
            # Durable keeper stop intent: no restart, not even at login, until
            # the selected image's explicit seed.
            root_macos.require_stopped(operation)

    def preflight_authority(self) -> GenerationRef:
        """Read-only: exactly one admitted write generation exists to fence."""
        from cli.release_transition import authority

        return authority.preflight()

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
        return self.candidate if operation.reference == self.request.candidate else self.previous

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
        holder, at = str(self.request.id), operation.maintenance_at
        current = maintenance.require_operation(holder, at)
        if current.maintenance is None:
            raise RuntimeError("release start lost its maintenance cohort")
        if current.maintenance.phase == "stopped":
            maintenance.set_phase(holder, at, "starting")
        elif current.maintenance.phase not in {"starting", "ready"}:
            raise RuntimeError("release start requires completed writer closure")
        if helper_root(operation.launch):
            from cli.release_transition import root_macos

            root_macos.start(journal, self.image(operation))
        else:
            from cli.release_transition.root_service import start

            start(operation, self.image(operation))

    def observe_root(self, operation: Operation) -> None:
        if helper_root(operation.launch):
            from cli.release_transition.root_macos import observe
        else:
            from cli.release_transition.root_service import observe
        observe(operation, self.image(operation))

    def observe(self, operation: Operation) -> None:
        self.request.require_configuration()
        from shared import maintenance

        self.observe_root(operation)
        self.request.require_configuration()
        holder, at = str(self.request.id), operation.maintenance_at
        current = maintenance.require_operation(holder, at)
        if current.maintenance is not None and current.maintenance.phase == "starting":
            maintenance.set_phase(holder, at, "ready")
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
        self.observe_root(operation)
        self.request.require_configuration()
        holder, at = str(self.request.id), operation.maintenance_at
        current = pause_owner.read()
        if (
            current.status == "resumed"
            and current.holder == holder
            and current.acquired_at == at
            and start_serving.is_serving()
        ):
            return
        maintenance.require_operation(holder, at)
        maintenance_commands.resume(holder, at, cancel=False)

    def restore(self, journal: Journal) -> None:
        """Abort: bring back the unchanged previous image on the unchanged generation.

        Nothing was fenced or selected. A drain that never stopped root is
        cancelled; otherwise the stop completes, the previous root starts,
        is observed and resumes, all under the same hold.
        """
        from cli.commands import maintenance as maintenance_commands
        from shared import pause_owner

        operation = journal.operation
        holder, at = str(self.request.id), operation.maintenance_at
        current = pause_owner.read()
        if not current.matches(holder, at) or current.status == "resumed":
            if current.status == "paused" and not current.matches(holder, at):
                raise RuntimeError("another maintenance owner holds this unit; restore refused")
            return  # never quiesced, or already restored and resumed
        hold = current.maintenance
        if hold is None or hold.phase in {"preparing", "draining", "drained"}:
            maintenance_commands.resume(holder, at, cancel=True)
            return
        if hold.phase == "stopping":
            self.stop(operation)
        self.start(journal)
        self.observe(journal.operation)
        self.resume(journal.operation)
