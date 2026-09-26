"""Finite same-image PITR effects under the existing home operation executor.

ActivationRecord owns business progress. This driver owns maintenance and native
stop/start boundaries, and never starts an application outside its boot owner.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from cli.release_transition.journal import Journal, Operation, read_operation
from cli.release_transition.pitr_evidence import PitrSeal
from cli.release_transition.pitr_inputs import read_record, require_inputs
from cli.release_transition.request import PitrRequest
from services.pitr.activation_state import ActivationRecord, record_path, write_record
from shared.native_process.ownership import OwnedProcess
from shared.process_evidence import ExpectedProcess
from shared.verified_file import regular_bytes


def _digest(home: Path) -> str:
    return hashlib.sha256(regular_bytes(record_path(home))).hexdigest()


def _pg_identity(state: dict[str, str]) -> ExpectedProcess:
    return ExpectedProcess(
        pid=int(state["postmaster_pid"]),
        create_time=float(state["postmaster_create_time"]),
        starttime=int(state["postmaster_starttime"]) if state["postmaster_starttime"] else None,
    )


def _require_pg_cluster(before: dict[str, str], after: dict[str, str]) -> None:
    if any(
        before[key] != after[key]
        for key in ("system_identifier", "data_directory", "port", "dbname")
    ):
        raise RuntimeError("PITR PostgreSQL cluster identity changed")


def observe_postgres(operation: Operation) -> None:
    """Require actual new native birth and owned archive settings after restart."""
    from cli.commands import _pitr_activation as activation
    from services.pitr.activation_runtime import _archive_settings, _settings_digest

    progress = operation.pitr
    if progress is None or progress.seal is None:
        raise RuntimeError("PITR observation has no sealed restart inputs")
    require_inputs(operation)
    current = activation._read_pg_state()
    _require_pg_cluster(progress.seal.postgres_state, current)
    before, after = progress.seal.postgres, _pg_identity(current)
    if OwnedProcess(before.pid, before.create_time, before.starttime).same_birth(
        OwnedProcess(after.pid, after.create_time, after.starttime)
    ):
        raise RuntimeError("PITR restart did not replace the captured PostgreSQL birth")
    if _settings_digest(_archive_settings(current)) != progress.seal.archive_settings_digest:
        raise RuntimeError("PostgreSQL did not load the sealed PITR archive settings")
    record = read_record(operation)
    if record is None:
        raise RuntimeError("PITR observation lost its activation record")
    activation._require_same_credentials(record.pre_activation_credential_evidence, "after restart")


def persist_failure(operation: Operation, failure: BaseException) -> None:
    """Business diagnostics remain in the activation journal under the same writer."""
    from services.pitr.activation_observability import save_error

    if operation.phase not in {"provisioning", "proving"}:
        return
    record = read_record(operation)
    if record is not None:
        save_error(Path(operation.request.home), record, failure)


class PitrTransition:
    def __init__(self, request: PitrRequest) -> None:
        self.request = request
        self.home = Path(request.home)
        self.image = request.image.verify(self.home, request.platform_tag)

    @property
    def at(self) -> datetime:
        return read_operation(self.request.path).maintenance_at

    def preflight(self) -> None:
        from cli.release_transition.identity import require_local_writers
        from shared.runtime_release import current_pointer

        self.request.require_configuration()
        if current_pointer(self.home / "releases") != self.request.image.selector:
            raise ValueError("PITR must use the currently selected retained image")
        require_local_writers(self.request)

    def _record(self, journal: Journal) -> ActivationRecord:
        operation, progress = journal.operation, journal.operation.pitr
        if progress is None:
            raise ValueError("PITR driver requires typed progress")
        record = read_record(operation)
        if record is None or (
            record.phase == "rolled_back"
            and progress.action == "activate"
            and record.operation_id != str(self.request.activation_id)
        ):
            if progress.action != "activate":
                raise ValueError("rollback has no activation record")
            record = ActivationRecord.start(
                operation_id=str(self.request.activation_id), origin=self.request.origin
            )
        if record.operation_id != str(self.request.activation_id):
            raise ValueError("PITR operation names a different activation")
        binding = (str(self.request.id), progress.action, progress.generation)
        if (record.home_operation, record.home_action, record.home_generation) != binding:
            record = replace(
                record,
                home_operation=binding[0],
                home_action=binding[1],
                home_generation=binding[2],
            )
            write_record(self.home, record)
        return record

    def provision(self, journal: Journal) -> bool:
        """Return true only when a fresh interpreter must enter the sealed phase."""
        from cli.commands import _pitr_activation as activation
        from services.pitr.activation_state import lock_path
        from shared.cluster_lock import acquire_update_lock, release_update_lock
        from shared.platform import file_lock

        require_inputs(journal.operation)
        progress = journal.operation.pitr
        if progress is None:
            raise ValueError("PITR provisioning has no action")
        with file_lock(lock_path(self.home), timeout_s=5):
            record = self._record(journal)
            if progress.action == "rollback" and self._data_down(journal.operation):
                record = self._offline_rollback(journal, record)
                self._seal(journal, record, data_stopped=True)
                return True
            holder = f"pitr:{self.request.id}:{progress.generation}"
            if not acquire_update_lock(holder, kind="update"):
                raise RuntimeError("another online writer owns the PostgreSQL mutation lease")
            try:
                if progress.action == "activate":
                    record = activation._advance_activation(
                        self.home, record, holder, stop_at_restart=True
                    )
                else:
                    record = activation._rollback_record(self.home, record)
            finally:
                release_update_lock(holder)
            if record.phase in {"protected", "rolled_back"}:
                journal.pitr_no_restart()
                return False
            if record.phase not in {"wal_restart_pending", "rollback_restart_pending"}:
                raise RuntimeError("PITR provisioning did not reach its restart boundary")
            self._seal(journal, record, data_stopped=False)
        return True

    def _data_down(self, operation: Operation) -> bool:
        from cli.commands._maintenance_data_plane import _capture_postgres

        return (
            operation.pitr is not None
            and operation.pitr.data_stop is not None
            and _capture_postgres() is None
        )

    def _offline_rollback(self, journal: Journal, record: ActivationRecord) -> ActivationRecord:
        from cli.commands._maintenance_data_plane import stop_captured
        from cli.commands._root_driver import _require_root_absent
        from services.pitr.activation_runtime import _restore_exact_file, _settings_digest
        from shared import maintenance

        progress = journal.operation.pitr
        if (
            progress is None
            or progress.data_stop is None
            or record.pre_activation_pg_settings is None
        ):
            raise RuntimeError("offline rollback has no captured data-plane custody")
        current = maintenance.require_operation(str(self.request.id), self.at)
        if current.maintenance is None or current.maintenance.phase not in {"stopped", "starting"}:
            raise RuntimeError("offline rollback requires the exact stopped maintenance generation")
        _require_root_absent()
        stop_captured(progress.data_stop, 90)
        if record.phase == "rollback_restart_pending":
            return record
        if record.phase != "rollback_pending":
            record = record.advance(
                "rollback_pending",
                wal_config_before_digest=_settings_digest(
                    {
                        name: record.pre_activation_pg_settings[name]
                        for name in (
                            "archive_mode",
                            "archive_command",
                            "archive_timeout",
                            "wal_compression",
                        )
                    }
                ),
                rollback_postmaster_started_at=record.pre_activation_pg_settings[
                    "postmaster_started_at"
                ],
            )
            write_record(self.home, record)
        if (
            record.pre_activation_auto_conf_b64 is None
            or record.pre_activation_auto_conf_digest is None
            or record.rollback_expected_auto_conf_digest is None
        ):
            raise RuntimeError("offline rollback lacks exact auto.conf byte ownership")
        _restore_exact_file(
            self.home / "pg/postgresql.auto.conf",
            payload_b64=record.pre_activation_auto_conf_b64,
            target_digest=record.pre_activation_auto_conf_digest,
            expected_digest=record.rollback_expected_auto_conf_digest,
        )
        digest = hashlib.sha256(regular_bytes(self.home / "pg/postgresql.auto.conf")).hexdigest()
        baseline = record.pre_activation_pg_auto_conf
        if baseline is None:
            raise RuntimeError("offline rollback lacks owned archive-setting baseline")
        record = record.advance(
            "rollback_restart_pending",
            rollback_expected_auto_conf_digest=digest,
            rollback_setting_intent=None,
            rollback_settings_applied={
                name: json.dumps({"desired_value": value, "post_digest": digest})
                for name, value in baseline.items()
            },
        )
        write_record(self.home, record)
        return record

    def _seal(self, journal: Journal, record: ActivationRecord, *, data_stopped: bool) -> None:
        from cli.commands import _pitr_activation as activation
        from services.pitr.activation_runtime import _archive_settings, _settings_digest
        from shared.start_inputs import configuration_digest

        require_inputs(journal.operation)
        state = record.pre_activation_pg_settings if data_stopped else activation._read_pg_state()
        if state is None:
            raise RuntimeError("PITR start seal lacks PostgreSQL identity")
        desired = (
            record.wal_config_desired_digest
            if record.home_action == "activate"
            else _settings_digest(_archive_settings(record.pre_activation_pg_settings or {}))
        )
        if desired is None or record.rollback_expected_auto_conf_digest is None:
            raise RuntimeError("PITR start seal lacks expected archive configuration")
        seal = PitrSeal(
            configuration_digest=configuration_digest(self.home),
            auto_conf_digest=record.rollback_expected_auto_conf_digest,
            archive_settings_digest=desired,
            postgres=_pg_identity(state),
            postgres_state={
                key: state[key] for key in ("system_identifier", "data_directory", "port", "dbname")
            },
            activation_digest=_digest(self.home),
        )
        journal.provisioned(seal, data_stopped=data_stopped)

    def quiesce(self, operation: Operation) -> None:
        from cli.release_transition.identity import require_local_writers
        from cli.release_transition.root_service import preflight
        from ops.agent_pause import _drain, _prepare

        require_inputs(operation)
        require_local_writers(self.request)
        preflight(operation, self.image, previous=False)
        from shared import maintenance

        current = maintenance.snapshot()
        if current is not None:
            if current.holder != str(self.request.id) or current.acquired_at != self.at:
                raise RuntimeError("PITR cannot adopt another maintenance generation")
            if current.maintenance is not None and current.maintenance.phase in {
                "drained",
                "stopping",
                "stopped",
                "starting",
                "ready",
            }:
                return
        _prepare(str(self.request.id), self.at)
        _drain(str(self.request.id), self.at, 90, reap=True)

    def stop_apps(self, journal: Journal) -> None:
        from cli.commands._maintenance import _stop
        from cli.commands._maintenance_data_plane import capture_custody
        from cli.commands._root_driver import _require_root_absent
        from shared import maintenance, pause_owner
        from shared.maintenance_state import MaintenanceHold

        require_inputs(journal.operation)
        current = maintenance.require_operation(str(self.request.id), self.at)
        hold = current.maintenance
        if hold is None:
            raise RuntimeError("PITR stop lost its captured maintenance cohort")
        if hold.phase in {"starting", "ready"}:
            pause_owner.change_maintenance(
                str(self.request.id),
                self.at,
                hold,
                MaintenanceHold.decode(hold.encode() | {"phase": "stopping"}),
            )
        _stop(str(self.request.id), self.at, 90, gateway_last=True)
        _require_root_absent()
        progress = journal.operation.pitr
        if progress is None:
            raise RuntimeError("PITR stop lost its action")
        if progress.data_stop is None:
            receipt = capture_custody(30)
            if progress.seal is None:
                raise RuntimeError("PITR data capture has no sealed PostgreSQL birth")
            before = progress.seal.postgres
            if not receipt.postgres.identity.same_birth(
                OwnedProcess(before.pid, before.create_time, before.starttime)
            ):
                raise RuntimeError("PostgreSQL was replaced before PITR acquired stop custody")
            journal.record_pitr(progress.model_copy(update={"data_stop": receipt}))

    def stop_data(self, operation: Operation) -> None:
        from cli.commands._maintenance_data_plane import stop_captured
        from cli.commands._root_driver import _require_root_absent
        from shared import maintenance

        require_inputs(operation)
        current = maintenance.require_operation(str(self.request.id), self.at)
        if current.maintenance is None or current.maintenance.phase != "stopped":
            raise RuntimeError("PITR data stop requires its stopped maintenance generation")
        _require_root_absent()
        if operation.pitr is None or operation.pitr.data_stop is None:
            raise RuntimeError("PITR data stop has no durable native receipt")
        stop_captured(operation.pitr.data_stop, 90)

    def start(self, operation: Operation) -> None:
        from cli.release_transition.root_service import start
        from shared import maintenance

        require_inputs(operation)
        current = maintenance.require_operation(str(self.request.id), self.at)
        if current.maintenance is None:
            raise RuntimeError("PITR start lost its maintenance generation")
        if current.maintenance.phase == "stopped":
            maintenance.set_phase(str(self.request.id), self.at, "starting")
        elif current.maintenance.phase not in {"starting", "ready"}:
            raise RuntimeError("PITR start requires completed native closure")
        start(operation, self.image)

    def observe(self, operation: Operation) -> None:
        from cli.release_transition.root_service import observe, restore_boot
        from shared import maintenance

        observe(operation, self.image)
        maintenance.set_phase(str(self.request.id), self.at, "ready")
        restore_boot(operation, self.image)

    def resume(self, operation: Operation) -> None:
        from cli.commands._maintenance import _resume
        from cli.release_transition.root_service import observe
        from shared import pause_owner, start_serving

        observe(operation, self.image)
        current = pause_owner.read()
        if (
            current.status == "resumed"
            and current.holder == str(self.request.id)
            and current.acquired_at == self.at
            and start_serving.is_serving()
        ):
            return
        _resume(str(self.request.id), self.at, cancel=False)

    def prove(self, journal: Journal) -> None:
        from cli.commands import _pitr_activation as activation
        from services.pitr.activation_state import lock_path
        from shared.cluster_lock import acquire_update_lock, release_update_lock
        from shared.platform import file_lock

        require_inputs(journal.operation)
        holder = f"pitr-proof:{self.request.id}"
        if not acquire_update_lock(holder, kind="update"):
            raise RuntimeError("PITR proof could not reserve its online writer lease")
        try:
            with file_lock(lock_path(self.home), timeout_s=5):
                record = self._record(journal)
                if record.home_action == "activate":
                    record = activation._advance_activation(self.home, record, holder)
                else:
                    record = activation._rollback_record(self.home, record)
                if record.phase not in {"protected", "rolled_back"}:
                    raise RuntimeError("PITR business proof remains incomplete")
        finally:
            release_update_lock(holder)
