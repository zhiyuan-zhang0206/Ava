"""Deployment-side companion of `services.ava_root` (W1.2e).

The root package stays generic and deployable anywhere; everything here
couples it to THIS deployment:

- `manifests` — the generator: ops roster -> K2 manifest JSON (validated by
  the root's own reader before anything consumes it);
- `glue` — the reference wiring the daemon drives through its explicit
  `--wiring module:attr` hook (probes from specs + the two 60s monitors);
- `drill` — the throwaway-tree wiring used by `scripts/ava_root_dry_run.py`.

None of this is imported by the root package: the seam is the wiring hook,
and it only fires when a launcher passes it explicitly.
"""

from __future__ import annotations
