"""Bootstrap helper shared by `read_trace.py` / `fetch_trace.py`: locate the
Ava source root so `base` is importable, before either script can import
`base.host.env.dotenv_boot.resolve_ava_home` to resolve `$AVA_HOME` itself.

Kept in its own file (rather than duplicated in both scripts, which is how
this landed originally) so there is exactly one place that resolves this
chicken-and-egg step: `resolve_ava_home` cannot run until `base` is on
`sys.path`, and `base` cannot be located without first deciding where to
look. A script-mode sibling import needs its own guard under
`PYTHONSAFEPATH=1` (see `scripts/lint/no_script_sibling_imports.py`) — both
callers insert their own directory onto `sys.path` before importing this
module.
"""

from __future__ import annotations

import os
from pathlib import Path


def source_root() -> Path:
    """The checkout / install root that holds the ``base`` package.

    The script is invoked from two places: the dev checkout (``.agents/
    skills/...`` — walk up to the repo root) and the prod install
    (``$AVA_HOME/skills/...`` — a converge copy; ``base`` lives in
    ``$AVA_HOME/source``). ``base.host.env.dotenv_boot`` must be importable from
    either, so the root is resolved before the import happens.

    The second case locates the home the way `resolve_ava_home` does (``AVA_HOME``
    when set, else ``~/.ava``); that function cannot run yet, because ``base``
    is not importable until this returns.
    """

    here = Path(__file__).resolve().parent
    for cand in (here, *here.parents):
        if (cand / "base" / "__init__.py").is_file():
            return cand
    env = os.environ.get("AVA_HOME")
    cand = (Path(env).expanduser() if env else Path.home() / ".ava") / "source"
    if (cand / "base" / "__init__.py").is_file():
        return cand
    raise RuntimeError(
        f"cannot locate the Ava source root: no `base` package above {here} and none at {cand}"
    )
