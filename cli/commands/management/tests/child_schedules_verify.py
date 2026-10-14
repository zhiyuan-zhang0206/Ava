"""The two python checks of `fleet_update._PRE_VERIFY`, without the shell around them.

Usage: `child_schedules_verify.py read_table <out file>` dumps the schedule rows as
JSON; `child_schedules_verify.py check_rows <rows file>` sweeps a rows file and exits
with the verify command's code.
"""

import json
import sys
from pathlib import Path

from base.paths import ava_home
from cli.commands.management.schedules_verify import _read_schedule_rows, cmd_schedules_verify
from cli.database import (
    open_operator_database,
    operator_database_factory,
    operator_event_pipeline,
)
from cli.database.runtime import CommandRuntime


def _read_table(out: str) -> None:
    command = CommandRuntime.create()
    database = operator_database_factory(
        local_host=command.machine.host, open_database=open_operator_database
    )
    with Path(out).open("w") as handle:
        json.dump(_read_schedule_rows(database_factory=database), handle)


def _check_rows(rows_file: str) -> None:
    command = CommandRuntime.create()
    database = operator_database_factory(
        local_host=command.machine.host, open_database=open_operator_database
    )
    producer = operator_event_pipeline(database, machine=command.machine, home=ava_home())
    try:
        sys.exit(
            cmd_schedules_verify(
                notify=False, rows_file=rows_file, database_factory=database, producer=producer
            )
        )
    finally:
        producer.close()


if __name__ == "__main__":
    if sys.argv[1] == "read_table":
        _read_table(sys.argv[2])
    elif sys.argv[1] == "check_rows":
        _check_rows(sys.argv[2])
    else:
        raise SystemExit(f"unknown scenario {sys.argv[1]!r}")
