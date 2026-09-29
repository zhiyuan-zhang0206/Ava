"""`ava cluster update --prepared REQUEST`: the previous image's half of the handoff.

It reads only the request's envelope, requires the request's home to be the
home this CLI resolves (settings-free), verifies the executor image in that
home's store against this host, takes this CLI's database authority, and
replaces this process with the executor's `submit` entry. Everything after the
exec is candidate code. The contract is `base.api_contracts.release_handoff`.

The executor image is not the home's selected image until its own operation
selects it, so no boot pass admits it to a write generation; its submission
reads the registered units with the login this admitted CLI hands over. This
CLI never builds Settings, so it takes that login here, exactly as its boot
pass would have (`base.host.env.dotenv_boot.operator_db_delivery`): the active
gateway login and its generation marker, in the exec environment only (never
argv, a file or a log), without the gateway API token. The executor's boot
pass keeps a delivery naming its home's endpoint. On a home without a ledger
nothing is added.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

from base.api_contracts.release_handoff import (
    RELEASE_REQUEST_MAX_BYTES,
    Envelope,
    HandoffRefusedError,
    entry_argv,
    entry_environment,
    read_envelope,
)
from base.deploy.release.runtime_release import VerifiedRelease
from base.deploy.release.verified_file import regular_bytes
from base.runtime_abi import current_abi


def _require_own_home(envelope: Envelope) -> None:
    from base.host.env import dotenv_boot

    home, anchored = dotenv_boot.resolve_ava_home()
    if not anchored:
        raise HandoffRefusedError(
            "this checkout owns no home; run the home's own `ava` or set AVA_HOME"
        )
    if Path(envelope.home) != home.expanduser().resolve():
        raise HandoffRefusedError(
            f"the request belongs to {envelope.home}, not to this CLI's home {home}"
        )


def _db_authority() -> dict[str, str]:
    """The database authority this CLI holds on its (already required) home."""
    from dotenv import dotenv_values

    from base.host.env import dotenv_boot

    files = {
        **dotenv_values(dotenv_boot.AVA_ENV_PATH),
        **dotenv_values(dotenv_boot.AVA_MIRROR_ENV_PATH),
    }
    delivery = dotenv_boot.operator_db_delivery(files.get("AVA_DB_URL"), api=False)
    if isinstance(delivery, str):
        raise HandoffRefusedError(delivery)
    return delivery


def _exec_submit(
    image: VerifiedRelease, envelope: Envelope, source: Path, authority: dict[str, str]
) -> NoReturn:
    os.chdir(image.cwd)
    os.execve(  # noqa: S606 — the verified executor image's fixed v1 entry, no shell
        image.interpreter,
        entry_argv(image, "submit", str(source)),
        entry_environment({**os.environ, **authority}, envelope.home),
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
        authority = _db_authority()
    except (ValueError, OSError, RuntimeError) as exc:
        sys.stderr.write(f"release update refused: {exc}\n")
        return 2
    _exec_submit(image, envelope, source, authority)
