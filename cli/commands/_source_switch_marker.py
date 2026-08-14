"""Source-switch marker CLI entry — `python -m cli.commands._source_switch_marker on|off`.

The Windows update ladder's seam into `shared.source_switch`: a cmd.exe chain
cannot import Python state, so the `on` / `off` verbs here are how the ladder
opens and closes the source-switch window around its `git checkout` (the same
placement the updater lease uses via `cli.commands._updater_lease`). The
in-process update path (`_update_agent_runner`) calls the module directly.

Both verbs are deliberately fail-soft in the shell chains (`|| ver>nul`), like
the lease: the marker only gates healthcheck respawns, so a write failure must
not block the update. Exit code 0 on success, 1 on any failure.
"""

from __future__ import annotations

import sys


def _main(argv: list[str]) -> int:
    from shared.source_switch import clear_switching, mark_switching

    verb = argv[1] if len(argv) > 1 else "on"
    if verb == "on":
        mark_switching()
        return 0
    if verb == "off":
        clear_switching()
        return 0
    print(f"_source_switch_marker: unknown verb {verb!r} (expected on|off)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
