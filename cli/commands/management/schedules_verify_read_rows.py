"""Read step of the pre-update schedule verify: dump the stored schedule rows to a file.

`cli.fleet_update` sends this file's text to a host as `python -c` and runs it under the home's
own (OLD) source checkout, so it may lean on nothing NEW-only: it dispatches on the signature of
`_read_schedule_rows` it finds, and a real error fails loudly instead of falling back.
Usage: `schedules_verify_read_rows.py OUT_FILE`.
"""

import inspect
import json
import sys
from pathlib import Path
from typing import Any

from cli.commands.management import schedules_verify as verify


def read_rows(out: str) -> None:
    rows: list[tuple[int, str, str]]
    if "database_factory" in inspect.signature(verify._read_schedule_rows).parameters:
        from cli.database import open_operator_database, operator_database_factory
        from cli.database.runtime import CommandRuntime

        command = CommandRuntime.create()
        if "gateway_url_reader" in inspect.signature(operator_database_factory).parameters:
            database_factory = operator_database_factory(
                local_host=command.machine.host,
                gateway_url_reader=lambda: command.config.view.gateway.gateway_url,
                open_database=open_operator_database,
            )
        else:
            legacy_factory: Any = operator_database_factory
            database_factory = legacy_factory(
                local_host=command.machine.host, open_database=open_operator_database
            )
        rows = verify._read_schedule_rows(database_factory=database_factory)
    else:
        legacy: Any = verify._read_schedule_rows
        rows = legacy()
    with Path(out).open("w") as handle:
        json.dump([list(row) for row in rows], handle)


if __name__ == "__main__":
    read_rows(sys.argv[1])
