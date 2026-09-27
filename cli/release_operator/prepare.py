"""`ava cluster release prepare` — build one inactive image on this host.

Thin wiring to `cli.release_prepare.prepare_image` (see
`cli/release_prepare/release_prepare.ava.okf.md`): this module resolves
`work`/`store` under `$AVA_HOME` and constructs the same `Preparation` request
the standalone `python -m cli.release_prepare` entry point takes, then calls
the same function. No release-preparation semantics are added or changed.

Building the `LocalInputs` this wraps — a managed Python tree, a flat
dependency wheelhouse, optionally a built frontend/collector/plugin tree — has
no host-convention resolver anywhere in this codebase yet. CI assembles them
by hand, step by step, in `.github/workflows/runtime-prepare.yml`, and
`cli.release_prepare.acquire` (`ava cluster release prepare` does not call it)
captures them online from a repo checkout for a different, not-yet-adopted
path. Rather than invent a third way to assemble them, this verb takes an
already-produced `LocalInputs` JSON — the exact document
`python -m cli.release_prepare` already required — as `--inputs`. Auto-
acquiring inputs from a bare `--commit` is future work, not this slice.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cli.release_operator.layout import prepare_work_dir, prepare_work_root, releases_store
from cli.release_prepare import LocalInputs, Preparation, prepare_image
from cli.release_prepare.models import encode
from shared.private_storage import ensure_private_dir
from shared.verified_file import regular_bytes


def cmd_release_prepare(*, commit: str, inputs: Path, repo: Path | None) -> int:
    from shared.paths import ava_home, repo_root

    home = ava_home()
    try:
        supplied = LocalInputs.model_validate_json(regular_bytes(inputs))
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"release prepare refused: could not read --inputs {inputs}: {exc}\n")
        return 2
    try:
        store = releases_store(home)
        ensure_private_dir(store)
        ensure_private_dir(prepare_work_root(home))
        work = prepare_work_dir(home, commit)
        request = Preparation(
            repo=(repo.resolve() if repo is not None else repo_root()),
            commit=commit,
            work=work,
            store=store,
            inputs=supplied,
        )
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"release prepare refused: {exc}\n")
        return 2
    try:
        receipt = prepare_image(request)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"release preparation failed: {exc}\n")
        return 1
    sys.stdout.write(encode(receipt).decode())
    return 0
