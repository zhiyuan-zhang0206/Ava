import contextlib
import sys

# `agent/__init__` is import-light (no `from .loop import ...`), so importing
# these two boot helpers does NOT pull the heavy chain — they just anchor the
# clock and arm the boot watchdog before the row claim.
from agent import _boot_deadline, _boot_timing

_boot_timing.mark("start")

# The two boot-watchdog windows are read and STRIPPED here, before any import:
# they arm the watchdog whose job is to cover the import chain, so they cannot
# come from `shared.config` (importing that IS a measurable chunk of what needs
# watching), and they must not survive into `agent/loop.py:run()`, whose parser
# is strict. The launcher owns the values (`ops/agent_launch.py`) and hands them
# down, so the two agree by construction rather than by both reading a setting.
_boot_stall_seconds, _boot_budget_seconds = _boot_deadline.consume_flags(sys.argv)

# Parse --agent-id from sys.argv BEFORE importing agent.loop (which triggers
# heavy langgraph imports). This lets us claim the unowned row early so the
# gateway's spawn poller returns quickly instead of
# waiting for the full langgraph import chain.
_agent_id = None
for _i, _arg in enumerate(sys.argv):
    if _arg == "--agent-id" and _i + 1 < len(sys.argv):
        with contextlib.suppress(ValueError):
            _agent_id = int(sys.argv[_i + 1])
        break

if _agent_id is not None:
    # Armed before the import below and disarmed after the CAS: across that whole
    # stretch the process guarantees "alive implies progressing", which is what
    # makes the launcher's liveness probe decisive. See agent/_boot_deadline.py.
    _boot_deadline.arm(_agent_id, _boot_stall_seconds, _boot_budget_seconds)

    from agent._starting import claim_agent_row_or_die_on_stale_schema
    from agent.restart_admission import consume_restart_command
    from shared.resource_birth import consume_birth_token
    from shared.runtime_admission import PublicationAdmissionDeferredError

    _boot_timing.mark("starting_import")
    try:
        claim_agent_row_or_die_on_stale_schema(
            _agent_id,
            restart_command_id=consume_restart_command(sys.argv),
            resurrect_command_id=consume_restart_command(sys.argv, flag="--resurrect-command-id"),
            resource_birth=consume_birth_token(sys.argv),
        )
    except PublicationAdmissionDeferredError:
        _boot_deadline.disarm()
        raise SystemExit(75) from None
    _boot_timing.mark("claim_row")
    _boot_deadline.disarm()

# E402: this import is deliberately placed after claiming the row above — it
# triggers the heavy langgraph chain we want to defer until the row
# is claimed.
from .loop import run  # noqa: E402

_boot_timing.mark("import")
run()
