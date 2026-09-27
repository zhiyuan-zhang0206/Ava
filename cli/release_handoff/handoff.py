"""`ava cluster update --prepared REQUEST`: the previous image's half of the handoff.

It reads only the request's envelope, requires the request's home to be the
home this CLI resolves (settings-free), verifies the executor image in that
home's store against this host, and replaces this process with the executor's
`submit` entry. Everything after the exec is candidate code. The contract is
`shared.api_contracts.release_handoff`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

from shared.api_contracts.release_handoff import (
    RELEASE_REQUEST_MAX_BYTES,
    Envelope,
    HandoffRefusedError,
    entry_argv,
    entry_environment,
    read_envelope,
)
from shared.runtime_abi import current_abi
from shared.runtime_release import VerifiedRelease
from shared.verified_file import regular_bytes


def _require_own_home(envelope: Envelope) -> None:
    from shared import dotenv_boot

    home, anchored = dotenv_boot.resolve_ava_home()
    if not anchored:
        raise HandoffRefusedError(
            "this checkout owns no home; run the home's own `ava` or set AVA_HOME"
        )
    if Path(envelope.home) != home.expanduser().resolve():
        raise HandoffRefusedError(
            f"the request belongs to {envelope.home}, not to this CLI's home {home}"
        )


def _exec_submit(image: VerifiedRelease, envelope: Envelope, source: Path) -> NoReturn:
    os.chdir(image.cwd)
    os.execve(  # noqa: S606 — the verified executor image's fixed v1 entry, no shell
        image.interpreter,
        entry_argv(image, "submit", str(source)),
        entry_environment(os.environ, envelope.home),
    )


def run(path: Path) -> int:
    """Hand one prepared request to its executor image's `submit` entry."""
    source = path.absolute()
    try:
        envelope = read_envelope(regular_bytes(source, max_bytes=RELEASE_REQUEST_MAX_BYTES))
        _require_own_home(envelope)
        image = envelope.executor.verify(Path(envelope.home), host_abi=current_abi())
        if os.name == "nt":
            raise HandoffRefusedError("the release handoff execs its executor; Windows has no exec")
    except (ValueError, OSError, RuntimeError) as exc:
        sys.stderr.write(f"release update refused: {exc}\n")
        return 2
    _exec_submit(image, envelope, source)
