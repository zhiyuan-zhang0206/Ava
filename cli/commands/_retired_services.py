"""Retire the old process reaper without restoring it to the service roster."""

from cli.commands._maintenance_stop import stop_services
from shared.cluster import session_name


def stop_retired_services(timeout: float) -> None:
    """Stop this home's recorded restarter before native drain or startup.

    The current roster cannot select a removed service. Use the same exact
    process identity and normal signal boundary as other services, with the
    caller's remaining deadline. A survivor or unverifiable record raises;
    neither a timeout nor startup authorizes force. Missing/dead records are
    harmless, and persistent terminals or other homes are never selected.
    """
    stop_services(
        timeout,
        keep_terminals=True,
        selected=frozenset({session_name("restarter")}),
    )
