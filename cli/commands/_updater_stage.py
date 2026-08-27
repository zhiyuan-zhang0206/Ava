"""Per-stage wall-clock markers for the updater log.

The Windows updater runs as one cmd.exe command line (``cmd /s /c``), and
cmd.exe expands ``%TIME%`` once per command line, at parse time - so every
``echo %TIME%`` in the ladder would print the same second. Each stage marker is
therefore a tiny Python invocation instead: ``python -m
cli.commands._updater_stage <stage>`` prints ``[updater] stage <stage> @
HH:MM:SS.mmm`` into the updater session's captured output. Boring, exact, and
unit-testable on CI (no cmd.exe quoting to get wrong).

The same module supplies ``now_marker()`` to the in-process updater
(``cli.commands._update_agent_runner``) and the restart ladder's stop/start
boundaries (``cli.commands.stop.cmd_restart``), so every updater log - POSIX
and Windows - subdivides checkout / uv / stop / start the same way, and the
marker shape is spelled in exactly one place. The wall clock is local
(cluster) time, matching the loguru ``HH:MM:SS.mmm`` prefix on the loguru lines
in the same file; stage durations are the differences between consecutive
markers.
"""

from __future__ import annotations

import sys
import time


def now_marker() -> str:
    """Wall-clock marker, `HH:MM:SS.mmm`, matching the loguru line prefix."""
    now = time.time()
    return f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int(now % 1 * 1000):03d}"


def stage_line(stage: str) -> str:
    """One updater-log stage marker line, e.g. `[updater] stage fetch @ 13:09:45.123`."""
    return f"[updater] stage {stage} @ {now_marker()}"


def main(argv: list[str] | None = None) -> int:
    """`python -m cli.commands._updater_stage <stage...>` - print one marker.

    The stage name is joined from the remaining argv so a caller can pass
    multi-word stage names; the ladder's own call sites pass a single fixed
    token (`fetch` / `uv-sync` / `restart` / `done`)."""
    stage = " ".join(sys.argv[1:] if argv is None else argv) or "?"
    print(stage_line(stage), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
