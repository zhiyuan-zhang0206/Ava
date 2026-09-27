"""The candidate image's handoff entry: `python -m cli.release_handoff ENTRY SOURCE`.

`SOURCE` is the request document's path, or `-` for the exact bytes on stdin.
The entry runs only as the image the request names as its executor, so the
code that acts is always the code the previous image verified. `submit` is the
home release journal's submission; `receipt` and `preflight` are a unit's
read-only answers to a fleet coordinator (`cli.release_fleet.entries`).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import get_args

from shared.api_contracts.release_handoff import (
    RELEASE_REQUEST_MAX_BYTES,
    Envelope,
    ReleaseImageEntry,
    read_envelope,
)
from shared.verified_file import regular_bytes


def _code_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read(source: str) -> bytes:
    if source != "-":
        return regular_bytes(Path(source), max_bytes=RELEASE_REQUEST_MAX_BYTES)
    data = sys.stdin.buffer.read(RELEASE_REQUEST_MAX_BYTES + 1)
    if len(data) > RELEASE_REQUEST_MAX_BYTES:
        raise ValueError("the request document exceeds the handoff size limit")
    return data


def _require_running_executor(envelope: Envelope) -> None:
    image = (Path(envelope.home) / "releases" / envelope.executor.artifact_digest).resolve()
    if not _code_root().is_relative_to(image):
        raise ValueError("this entry runs only as the image the request names as its executor")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in get_args(ReleaseImageEntry):
        sys.stderr.write(
            f"usage: python -m cli.release_handoff {{{','.join(get_args(ReleaseImageEntry))}}} "
            "REQUEST|-\n"
        )
        return 2
    entry, source = args
    # A settings-full CLI process like `ava` itself, also when an ops server
    # (a launched service carrying its own process profile) runs the entry.
    from cli.main import _normalize_process_profile

    _normalize_process_profile()
    try:
        encoded = _read(source)
        _require_running_executor(read_envelope(encoded))
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"release handoff refused: {exc}\n")
        return 2
    if entry == "submit":
        from cli.release_transition.submit import run

        return run(encoded)
    from cli.release_fleet.entries import run as answer

    return answer(entry, encoded)


if __name__ == "__main__":
    raise SystemExit(main())
