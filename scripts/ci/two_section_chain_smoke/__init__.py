"""Two-section chain smoke: launchd -> permissions-helper -> ava-root -> unit.

The dev-side acceptance run for the macOS "two-section" adapter (task #3209, design #3195).
A helper compiled from this checkout is registered as a throwaway launchd job under an
isolated workdir; the helper seeds a dev ava-root from a seed config (the K3 face), and
this script verifies the whole chain plus the keeper's crash semantics:

  build      compile + ad-hoc sign a dev helper from this checkout
  launch     bootstrap the throwaway launchd job, wait for the helper
  chain      launchd -> helper -> root -> unit parentage via ps + root status
  attribute  a unit's TCCAccessPreflight requests resolve to the helper (F11)
  conflict   kill -9 the helper: launchd relaunches it; the relaunched helper
             finds the orphan root and rests in `conflict` (no double-spawn,
             no signal to the foreign PID); `root_stop` is refused with or
             without force; native recovery through the orphan's own
             `shutdown` verb closes its tree, and the keeper seeds a fresh
             root on the freed run dir as the relaunched helper's child; with
             --sample-conflict the phase also samples the whole tree chain +
             TCC attribution across the helper death/replacement window
             (F12b, task #3380)
  restart    kill -9 the root: its units keep running (the design's "lose
             attribution, not service"); the keeper relaunches root, and the
             replacement refuses cold start while the killed generation's
             service custody is unresolved, so no duplicate tree is born; with
             --sample-restart the phase also samples the surviving units'
             chain + TCC attribution around the crash (F12, task #3377)

  reconcile  (--reconcile-case) the surviving generation is killed under its
             stale custody records; the keeper's next cold-start attempt must
             reconcile them (release) and bring up a fresh generation, never
             refuse (task #4872, C-6 replay)

The helper binds its home to the seed file's directory, so the seed lives in the root run dir.
Nothing here touches production: the binary is throwaway-signed, every path lives under the workdir,
and the launchd job uses its own test label (never the production helper's). The helper's first-run
registration nudge is disabled via AVA_PERMISSIONS_HELPER_SKIP_REGISTRATION=1 -- an Aqua-session
helper with a fresh code identity would otherwise raise TCC dialogs on an unattended machine.

Exit 0 = every phase passed. Evidence (ps/logs/status snapshots) is retained
under the workdir; pass --cleanup to remove it. Run with the repository venv,
on macOS, from a checkout that contains services/ava_root (dev/CI only).
"""

# The package facade: callers keep using `two_section_chain_smoke.<name>` (the
# module's own surface before the split) — re-exported unchanged.
from .cli import main as main  # noqa: I001 - one re-export block, kept as written
from .f12 import (
    _f12_attribution_summary as _f12_attribution_summary,
    _f12_expect_attribution as _f12_expect_attribution,
    _f12_join as _f12_join,
    _f12_parse_window as _f12_parse_window,
)
from .support import SmokeError as SmokeError
