"""Bootstrap helper shared by `read_trace.py` / `fetch_trace.py`: locate the
Ava source root so `shared` is importable, before either script can import
`shared.dotenv_boot.resolve_ava_home` to resolve `$AVA_HOME` itself.

Kept in its own file (rather than duplicated in both scripts, which is how
this landed originally) so there is exactly one place that resolves this
chicken-and-egg step: `resolve_ava_home` cannot run until `shared` is on
`sys.path`, and `shared` cannot be located without first deciding where to
look. A script-mode sibling import needs its own guard under
`PYTHONSAFEPATH=1` (see `scripts/lint/no_script_sibling_imports.py`) — both
callers insert their own directory onto `sys.path` before importing this
module.
"""

from __future__ import annotations

import os
from pathlib import Path


def source_root() -> Path:
    """The checkout / install root that holds the ``shared`` package.

    The script is invoked from two places: the dev checkout (``.agents/
    skills/...`` — walk up to the repo root) and the prod install
    (``$AVA_HOME/skills/...`` — a converge copy; ``shared`` lives in
    ``$AVA_HOME/source``). ``shared.dotenv_boot`` must be importable from
    either, so the root is resolved before the import happens.

    The second case needs an explicit ``AVA_HOME`` — never a
    ``Path(os.environ.get("AVA_HOME", "~/.ava"))`` guess. A bare interpreter
    running the converged copy with no `AVA_HOME` in its environment and a
    real `~/.ava/source` alongside (this machine, e.g., a worktree-cluster
    invocation that lost its inherited env) must never silently anchor to
    that default cluster instead of refusing — the same "unanchored checkout
    reaches production" bug class this module exists to resolve for
    `read_trace.py` / `fetch_trace.py` themselves, one level down (PR #3550's
    P2 follow-up).
    """

    here = Path(__file__).resolve().parent
    for cand in (here, *here.parents):
        if (cand / "shared" / "__init__.py").is_file():
            return cand
    env = os.environ.get("AVA_HOME")
    if not env:
        raise RuntimeError(
            f"cannot locate the Ava source root: no `shared` package above {here}, "
            "and AVA_HOME is not set to try <AVA_HOME>/source -- this never guesses "
            "~/.ava (see the P2 fix in #3550's follow-up); pass an explicit "
            "AVA_HOME=<cluster home> when running the converged skill copy with a "
            "bare interpreter."
        )
    cand = Path(env).expanduser() / "source"
    if (cand / "shared" / "__init__.py").is_file():
        return cand
    raise RuntimeError(
        f"cannot locate the Ava source root: no `shared` package above {here} and none at {cand}"
    )
