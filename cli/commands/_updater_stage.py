"""Emit a `[updater] stage=<name> t=<monotonic>` marker for the cmd.exe updater ladder.

The Windows updater runs as one cmd.exe command line (`ops.cluster_deploy`'s
native_cmd), so per-step durations cannot come from Python context managers
around each step. This module is the ladder's seam: `spawn_update` inserts
`& (python -m cli.commands._updater_stage <name> || ver>nul) &` between steps,
and each marker prints its monotonic timestamp into the same log stream the
supervisor redirects. `ops.updater_outcome._parse_stages` pairs consecutive
markers into per-stage durations.

Fail-soft by construction: a marker that fails to print (module missing on a
pre-split tree, interpreter hiccup) costs the reader a stage boundary, never
the update — `|| ver>nul` keeps the chain moving exactly like the source-switch
markers it sits beside. A marker that never gets to print because the step
before it failed is itself the diagnosis: the last marker in the log names the
step the chain died in.
"""

from __future__ import annotations

import sys
import time


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    name = args[0] if args else "unknown"
    print(f"[updater] stage={name} t={time.monotonic():.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
